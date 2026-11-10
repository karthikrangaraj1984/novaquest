"""Reading scanned books (SY-5): when a PDF's pages are pictures (no text layer), Tesseract OCR reads the words on
each page so the book can be outlined like any other.

Each page is rendered once with Poppler's pdftoppm (about 2,000 pixels wide, greyscale) and read by Tesseract with
one thread per process; a few pages are read at a time. Every page's text is saved next to the PDF as soon as it
is read, so a restarted worker carries on where it stopped and a book is never read twice. Pure file functions
(no database), so they are easy to test."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Callable

log = logging.getLogger("novaquest.ocr")

EMPTY_WORDS = 10      # a page with fewer words than this has no usable text layer
WIDTH_PX = 2000       # rendering width: small print on a 1,000-pixel scan is read far better at this size
PAGE_TIMEOUT_S = 120  # one page normally takes 2-4 seconds
_WINDOWS_DIRS = (r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR")


def tesseract() -> str | None:
    """The Tesseract program: on the PATH (Linux, or NQ_EXTRA_PATH), NQ_TESSERACT, or where the Windows
    installer puts it."""
    found = shutil.which("tesseract")
    if found:
        return found
    named = os.environ.get("NQ_TESSERACT", "")
    if named and Path(named).is_file():
        return named
    local = os.environ.get("LOCALAPPDATA", "")
    for d in (*_WINDOWS_DIRS, *([str(Path(local) / "Programs" / "Tesseract-OCR")] if local else [])):
        exe = Path(d) / "tesseract.exe"
        if exe.is_file():
            return str(exe)
    return None


class OCRFailed(RuntimeError):
    """Text recognition couldn't read any page; the message says why (Tesseract's own words)."""


def tessdata() -> str | None:
    """NovaQuest's own folder of Tesseract language models (NQ_TESSDATA, or where the Windows installer puts it), when
    it holds English. The official Windows Tesseract installer, run silently, installs no language at all."""
    local = os.environ.get("LOCALAPPDATA", "")
    for d in (os.environ.get("NQ_TESSDATA", ""), str(Path(local) / "NovaQuest" / "tessdata") if local else ""):
        if d and (Path(d) / "eng.traineddata").is_file():
            return d
    return None


def _lang_args() -> list[str]:
    d = tessdata()
    return ["--tessdata-dir", d] if d else []


@lru_cache(maxsize=4)
def _has_english(exe: str, data: str | None) -> bool:
    try:
        res = subprocess.run([exe, "--list-langs", *(["--tessdata-dir", data] if data else [])], capture_output=True,
                             timeout=30)
    except (subprocess.SubprocessError, OSError):
        return False
    return "eng" in res.stdout.decode("utf-8", "replace").split()


def problem() -> str:
    """Why scanned books can't be read on this computer, or '' when they can."""
    exe = tesseract()
    if not exe:
        return "Tesseract OCR isn't installed"
    if not shutil.which("pdftoppm"):
        return "Poppler's pdftoppm isn't installed"
    if not _has_english(exe, tessdata()):
        return "Tesseract has no English language file"
    return ""


def available() -> bool:
    """Both tools needed to read a scanned page are installed, and Tesseract can read English."""
    return problem() == ""


def scanned_pages(pages: list[str]) -> list[int]:
    """The 0-based pages to read with OCR: the empty pages of a file that is mostly pictures. A text PDF with a
    few blank or full-page-picture pages returns nothing (those pages hold no text to find)."""
    empty = [i for i, p in enumerate(pages) if len(p.split()) < EMPTY_WORDS]
    return empty if pages and len(empty) * 2 > len(pages) else []


def _cache(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".ocr")


def _read_page(pdf: Path, index: int, exe: str, renderer: str, out: Path) -> str:
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}  # one thread each: parallel pages are much faster than threads
    with tempfile.TemporaryDirectory(prefix="nq-ocr-") as tmp:
        base = Path(tmp) / "p"
        n = str(index + 1)
        shot = subprocess.run([renderer, "-f", n, "-l", n, "-scale-to-x", str(WIDTH_PX), "-scale-to-y", "-1", "-gray",
                               "-png", "-singlefile", str(pdf), str(base)], capture_output=True, timeout=PAGE_TIMEOUT_S)
        if shot.returncode != 0:
            why = " ".join(shot.stderr.decode("utf-8", "replace").split())[-300:] or f"exit status {shot.returncode}"
            raise OCRFailed(f"pdftoppm couldn't draw the page: {why}")
        res = subprocess.run([exe, str(base) + ".png", "stdout", *_lang_args(), "-l", "eng"], capture_output=True,
                             timeout=PAGE_TIMEOUT_S, env=env)
        if res.returncode != 0:  # keep Tesseract's own reason ('Failed loading language eng'), not just the exit code
            why = " ".join(res.stderr.decode("utf-8", "replace").split())[-300:] or f"exit status {res.returncode}"
            raise OCRFailed(f"Tesseract couldn't read the page: {why}")
    text = res.stdout.decode("utf-8", "replace").replace("\f", "").strip()
    part = out.with_suffix(".part")
    part.write_text(text, encoding="utf-8")
    part.replace(out)
    return text


def read_scanned(pdf: Path, pages: list[str], on_page: Callable[[int, int], None] | None = None,
                 workers: int | None = None) -> list[str]:
    """`pages` (the PDF's text layer) with every scanned page replaced by its OCR text. `on_page(done, total)`
    reports progress. Pages read before (saved next to the PDF) are not read again."""
    todo = scanned_pages(pages)
    if not todo:
        return pages
    exe, renderer = tesseract(), shutil.which("pdftoppm")
    if not exe or not renderer:
        raise OCRFailed("Tesseract OCR or Poppler's pdftoppm is not installed")
    cache = _cache(pdf)
    cache.mkdir(exist_ok=True)
    out = list(pages)
    missing = []
    for i in todo:
        f = cache / f"{i + 1:04d}.txt"
        if f.is_file():
            out[i] = f.read_text(encoding="utf-8")
        else:
            missing.append(i)
    done, total = len(todo) - len(missing), len(todo)
    if on_page:
        on_page(done, total)
    failed: list[str] = []
    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1 or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:  # each page is its own pair of processes
        futures = {i: pool.submit(_read_page, pdf, i, exe, renderer, cache / f"{i + 1:04d}.txt") for i in missing}
        for i in missing:
            try:
                out[i] = futures[i].result()
            except (subprocess.SubprocessError, OSError, OCRFailed) as exc:  # one unreadable page doesn't stop the book
                log.warning("OCR failed for %s page %s: %s", pdf.name, i + 1, exc)
                failed.append(str(exc))
                out[i] = ""
            done += 1
            if on_page and (done % 5 == 0 or done == total):
                on_page(done, total)
    if missing and len(failed) == len(missing) and len(missing) >= min(3, total):
        raise OCRFailed(failed[0])  # not one page could be read: the cause is the setup, not the scan
    return out

