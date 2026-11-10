"""The book's own figures (1.29.0), cut out of its printed pages for teachers' slides.

A figure is found from the PDF itself: Poppler's pdftohtml lists where each picture sits on a page and where each line
of text is. Pictures that belong together (the parts of 'Fig. 2.3 (a)…(d)') are joined, the caption next to them
('Fig. 2.3: Removing onion peel…') is attached, and the area is cut from the page with pdftoppm, so labels and arrows
drawn over the picture come along. Page backgrounds, watermarks and small icons are left out.

Only for books whose figures may be shown: the shared NCERT book, and a school's own book once its admin has confirmed
teachers may show its figures (or its full pages to students). Each page is read once and the cut-outs are cached on
disk."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from novaquest.core.db import Tx
from novaquest.core.errors import NotFound
from novaquest.services import pages, syllabus

log = logging.getLogger("novaquest.figures")
DPI = 130
MAX_PER_TOPIC = 6
_CAPTION = re.compile(r"^\s*(Fig(?:ure)?\.?\s*\d+(?:\.\d+)*[a-z]?)\s*[:.]?", re.I)
_lock = threading.Lock()


def allowed(book: dict[str, Any]) -> bool:
    """The shared NCERT book; a school's own book when its admin allowed its figures on teachers' slides, or its full
    pages for students."""
    return pages.allowed(book) or bool((book.get("meta") or {}).get("teacher_figures"))


def _box(el: ET.Element) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) of an XML element; pictures drawn flipped have negative sizes."""
    x, y, w, h = (float(el.get(k, 0)) for k in ("left", "top", "width", "height"))
    return (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))


def _near(a: tuple, b: tuple, gap: float) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _union(a: tuple, b: tuple) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def find(xml_text: str) -> dict[int, list[dict[str, Any]]]:
    """Figures on each page of pdftohtml's XML: {page number: [{'box': (l, t, r, b) in points, 'caption': str}]}."""
    root = ET.fromstring(xml_text)
    pages_ = root.findall("page")
    seen: dict[tuple, int] = {}
    for pg in pages_:  # a picture at the same place on most pages is a background or a watermark
        for im in pg.findall("image"):
            key = tuple(round(v) for v in _box(im))
            seen[key] = seen.get(key, 0) + 1
    out: dict[int, list[dict[str, Any]]] = {}
    for pg in pages_:
        pw, ph = float(pg.get("width", 600)), float(pg.get("height", 840))
        area = pw * ph
        boxes = []
        for im in pg.findall("image"):
            b = _box(im)
            w, h = b[2] - b[0], b[3] - b[1]
            if (len(pages_) > 1 and seen[tuple(round(v) for v in b)] > 1) or w * h > 0.5 * area or w < 30 or h < 30:
                continue
            if (b[1] < 0.08 * ph or b[3] > 0.93 * ph) and h < 0.12 * ph:  # running heads, page numbers, footers
                continue
            boxes.append(b)
        merged: list[tuple] = []
        for b in sorted(boxes, key=lambda b: (b[1], b[0])):  # the parts of one figure sit close together
            for i, m in enumerate(merged):
                if _near(b, m, 14):
                    merged[i] = _union(b, m)
                    break
            else:
                merged.append(b)
        changed = True
        while changed:  # joining two parts can bring a third within reach
            changed = False
            for i in range(len(merged)):
                for j in range(i + 1, len(merged)):
                    if _near(merged[i], merged[j], 14):
                        merged[i] = _union(merged[i], merged.pop(j))
                        changed = True
                        break
                if changed:
                    break
        lines = []
        for t in pg.findall("text"):
            txt = "".join(t.itertext()).strip()
            if txt:
                lines.append((_box(t), txt, t.get("font")))
        figs = []
        for b in merged:
            w, h = b[2] - b[0], b[3] - b[1]
            if w * h < 0.015 * area:
                continue
            caps = [(lb, txt, fo) for lb, txt, fo in lines if _CAPTION.match(txt) and _near(lb, b, 60)]
            caption, box = "", b
            if caps:
                lb, txt, font = min(caps, key=lambda c: min(abs(c[0][1] - b[3]), abs(c[0][3] - b[1])))
                caption = txt
                box = _union(box, lb)
                for lb2, txt2, font2 in sorted(lines, key=lambda c: c[0][1]):  # a caption running on to the next lines
                    if (font2 == font and -5 <= lb2[1] - lb[3] < 12 and lb2[1] > lb[1] and lb2[0] < lb[2] and lb2[2] > lb[0]
                            and not _CAPTION.match(txt2)):
                        caption += " " + txt2
                        box = _union(box, lb2)
                        lb = lb2
            figs.append({"box": box, "caption": re.sub(r"\s+", " ", caption).strip()[:200]})
        named = [f for f in figs if f["caption"]]
        if named:  # parts of a figure that sit a little apart (Fig. 2.2 (a)) join the figure; other pictures are decoration
            for f in figs:
                if not f["caption"]:
                    near = [g for g in named if _near(f["box"], g["box"], 40)]
                    if near:
                        g = min(near, key=lambda g: abs(g["box"][1] - f["box"][1]))
                        g["box"] = _union(g["box"], f["box"])
            by_caption: dict[str, dict] = {}
            for f in named:  # a figure in several pictures, (a) to (d), under one caption: one figure
                key = _CAPTION.match(f["caption"]).group(1).lower().replace(" ", "")
                if key in by_caption:
                    by_caption[key]["box"] = _union(by_caption[key]["box"], f["box"])
                    if len(f["caption"]) > len(by_caption[key]["caption"]):
                        by_caption[key]["caption"] = f["caption"]
                else:
                    by_caption[key] = f
            figs = list(by_caption.values())
        else:  # a book without 'Fig.' captions: its larger pictures
            figs = [f for f in figs if (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]) >= 0.05 * area]
        pad = 6
        for f in figs:
            b = f["box"]
            f["box"] = (max(0, b[0] - pad), max(0, b[1] - pad), min(pw, b[2] + pad), min(ph, b[3] + pad))
        out[int(pg.get("number", 0))] = sorted(figs, key=lambda f: (f["box"][1], f["box"][0]))
    return out


def _source(tx: Tx, topic: dict[str, Any], page: int, data_dir: Path) -> tuple[Path, int] | None:
    book = syllabus.get_syllabus(tx, topic["syllabus_id"])
    return pages._ncert_source(topic, page) if book["institution_id"] is None else pages._school_source(tx, topic, page, data_dir)


def _cache(data_dir: Path, pdf: Path, index: int, what: str) -> Path:
    key = hashlib.sha256(f"{pdf.resolve()}|{pdf.stat().st_mtime_ns}|{index}|{what}".encode()).hexdigest()[:32]
    return data_dir / "figcache" / key[:2] / key


def _read(pdf: Path, first: int, last: int, data_dir: Path) -> None:
    """Read pages first..last of a PDF in one go (with a page either side, to spot backgrounds) and cache each page's
    figures."""
    exe = shutil.which("pdftohtml")
    if not exe:
        return
    with tempfile.TemporaryDirectory() as tmp:
        res = subprocess.run([exe, "-xml", "-zoom", "1", "-q", "-f", str(max(1, first - 1)), "-l", str(last + 1), str(pdf),
                              str(Path(tmp) / "p")], capture_output=True, timeout=120)
        xml_file = Path(tmp) / "p.xml"
        if res.returncode != 0 or not xml_file.exists():
            log.warning("pdftohtml failed for %s pages %s-%s", pdf.name, first, last)
            return
        try:
            found = find(xml_file.read_text(encoding="utf-8", errors="replace"))
        except ET.ParseError:
            log.warning("pdftohtml gave bad XML for %s pages %s-%s", pdf.name, first, last)
            return
    for index in range(first, last + 1):
        if index in found:
            cached = _cache(data_dir, pdf, index, "list").with_suffix(".json")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(found[index]), encoding="utf-8")


def _page_figures(pdf: Path, index: int, data_dir: Path) -> list[dict[str, Any]]:
    """The figures on one page of a PDF, cached."""
    cached = _cache(data_dir, pdf, index, "list").with_suffix(".json")
    if not cached.exists():
        _read(pdf, index, index, data_dir)
    return json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else []


def _sources(tx: Tx, topic: dict[str, Any], data_dir: Path) -> dict[int, tuple[Path, int]]:
    out = {}
    for page in pages.topic_pages(tx, topic["id"]):
        src = _source(tx, topic, page, data_dir)
        if src and src[0].exists():
            out[page] = src
    return out


def _cached(data_dir: Path, src: tuple[Path, int]) -> bool:
    return _cache(data_dir, src[0], src[1], "list").with_suffix(".json").exists()


def ready(tx: Tx, topic_id: int, data_dir: Path) -> bool:
    """True when the topic's pages have been read for figures (or there's nothing to read)."""
    topic = syllabus.get_topic(tx, topic_id)
    if not allowed(syllabus.get_syllabus(tx, topic["syllabus_id"])) or not pages.renderer():
        return True
    return all(_cached(data_dir, s) for s in _sources(tx, topic, data_dir).values())


def _mentions(tx: Tx, chapter_id: int) -> dict[int, set[str]]:
    """The figure numbers each topic of a chapter mentions in its text ('Fig. 2.7' → '2.7')."""
    out: dict[int, set[str]] = {}
    for r in tx.all("""SELECT p.node_id, p.body FROM source_passages p JOIN syllabus_nodes n ON n.id=p.node_id
                       WHERE n.parent_id=%s AND n.kind='topic' AND p.source_kind='textbook'""", [chapter_id]):
        out.setdefault(r["node_id"], set()).update(re.findall(r"Fig(?:ure)?\.?\s*(\d+\.\d+)", r["body"]))
    return out


def topic_figures(tx: Tx, topic_id: int, data_dir: Path, read: bool = True) -> list[dict[str, Any]]:
    """A topic's figures from its printed pages, in page order: [{'page', 'n', 'caption'}] (n counts from 1 on the
    page). A figure on a page two topics share goes to the topic whose text mentions it ('as shown in Fig. 2.7'), else
    to the first of them. With read=False only pages already read are used (pages are read in the background, so a
    page never waits for it). Empty when the book's figures may not be shown, or its PDF isn't on this computer."""
    topic = syllabus.get_topic(tx, topic_id)
    book = syllabus.get_syllabus(tx, topic["syllabus_id"])
    if not allowed(book) or not pages.renderer():
        return []
    srcs = _sources(tx, topic, data_dir)
    if read:
        by_pdf: dict[Path, list[int]] = {}
        for src in srcs.values():
            if not _cached(data_dir, src):
                by_pdf.setdefault(src[0], []).append(src[1])
        for pdf, idx in by_pdf.items():  # one read per file for the pages not seen before
            try:
                _read(pdf, min(idx), max(idx), data_dir)
            except (OSError, subprocess.SubprocessError):
                log.exception("reading figures in %s failed", pdf.name)
    mentions = _mentions(tx, topic["chapter_id"])
    siblings = tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' ORDER BY ord", [topic["chapter_id"]])
    first_with = {}
    for sib in siblings:
        for page in pages.topic_pages(tx, sib["id"]):
            first_with.setdefault(page, sib["id"])
    out = []
    for page, src in srcs.items():
        if not _cached(data_dir, src):
            continue
        for i, f in enumerate(_page_figures(src[0], src[1], data_dir), 1):
            m = re.search(r"(\d+\.\d+)", f["caption"] or "")
            owners = {t for t, nums in mentions.items() if m and m.group(1) in nums}
            if (owners and topic_id not in owners) or (not owners and first_with.get(page) != topic_id):
                continue
            out.append({"page": page, "n": i, "caption": f["caption"]})
    return out[:MAX_PER_TOPIC]


JOB = "book_figures"


def warm(db: Any, inst: int, syllabus_id: int, data_dir: Path) -> int:
    """The background job: read every topic's pages for figures and cut them out, so teachers' pages open at once.
    Returns how many figures are ready."""
    with db.tenant(inst) as tx:
        topics = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic' ORDER BY ord",
                                          [syllabus_id])]
    n = 0
    for tid in topics:
        with db.tenant(inst) as tx:
            figs = topic_figures(tx, tid, data_dir, read=True)
            for f in figs:
                try:
                    image(tx, tid, f["page"], f["n"], data_dir)
                    n += 1
                except NotFound:
                    continue
    return n


def queue_warm(tx: Tx, syllabus_id: int) -> None:
    """Read the book's figures in the background (at most once an hour per book)."""
    from novaquest.core import clock
    from novaquest.services import jobs

    jobs.enqueue(tx, JOB, {"syllabus_id": syllabus_id}, dedupe_key=f"{JOB}:{syllabus_id}:{clock.now():%Y%m%d%H}")


def image(tx: Tx, topic_id: int, page: int, n: int, data_dir: Path) -> Path:
    """The JPEG of one figure, cut from its page once and then served from the cache."""
    topic = syllabus.get_topic(tx, topic_id)
    book = syllabus.get_syllabus(tx, topic["syllabus_id"])
    if not allowed(book) or page not in pages.topic_pages(tx, topic_id):
        raise NotFound("That figure isn't available.")
    src = _source(tx, topic, page, data_dir)
    exe = pages.renderer()
    if not src or not src[0].exists() or not exe:
        raise NotFound("The book isn't available on this computer.")
    pdf, index = src
    figs = _page_figures(pdf, index, data_dir)
    if not 1 <= n <= len(figs):
        raise NotFound("That figure isn't available.")
    out = _cache(data_dir, pdf, index, f"fig{n}").with_suffix(".jpg")
    if out.exists():
        return out
    left, top, right, bottom = figs[n - 1]["box"]
    k = DPI / 72
    out.parent.mkdir(parents=True, exist_ok=True)
    base = out.with_name(out.stem + f".{threading.get_ident()}")
    with _lock:
        if out.exists():
            return out
        res = subprocess.run([exe, "-f", str(index), "-l", str(index), "-r", str(DPI), "-x", str(int(left * k)),
                              "-y", str(int(top * k)), "-W", str(int((right - left) * k)), "-H", str(int((bottom - top) * k)),
                              "-jpeg", "-jpegopt", "quality=82", "-singlefile", str(pdf), str(base)], capture_output=True, timeout=60)
        made = base.with_suffix(base.suffix + ".jpg")
        if res.returncode != 0 or not made.exists():
            raise NotFound("That figure couldn't be shown.")
        made.replace(out)
    return out
