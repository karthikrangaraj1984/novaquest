"""Turn the textbook PDF into cited passages, one set per syllabus section.

Pure functions only (no database), so they are easy to test and reuse for other books."""
from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

_NOISE = re.compile(r"^\s*(\d{4}-\d{2}|[ivxlc]+|\d{1,3}|SCIENCE)\s*$", re.I)
_SKIP_FROM = re.compile(r"^\s*(Exercises|Extended Learning|EXERCISES|KEYWORDS)\b")


@dataclass(frozen=True, slots=True)
class Passage:
    """One chunk of textbook text with where it came from."""

    section: str
    page: int
    body: str


def extract_pages(pdf_path: Path, maths: bool = False) -> list[str]:
    """Return the text of every PDF page, using pdftotext when available (cleaner), else pypdf. For a maths book,
    powers and stacked fractions are read from the words' positions (see extract_maths_pages)."""
    if maths and shutil.which("pdftotext"):
        try:
            return [strip_figure_labels(p) for p in extract_maths_pages(pdf_path)]
        except (subprocess.SubprocessError, ValueError, OSError):
            pass  # the plain reading below still works
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"], check=True,
                             capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
        pages = out.split("\f")
        return [strip_figure_labels(p) for p in (pages[:-1] if pages and not pages[-1].strip() else pages)]
    import pypdf  # imported lazily: only needed without poppler

    reader = pypdf.PdfReader(str(pdf_path))
    raw = [page.extract_text() or "" for page in reader.pages]
    return [strip_figure_labels(p) for p in repair_split_words(raw)]


# ---------- maths (1.24.0) ----------
_SUP = str.maketrans("0123456789+-−–n()=ixy", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁻ⁿ⁽⁾⁼ⁱˣʸ")
_SUP_OK = re.compile(r"^[0-9+\-−–n()=ixy]{1,4}$")
_EXP_LIKE = re.compile(r"^[0-9a-zA-Z+\-−–]{1,4}$")
_FRAC_PART = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?|[a-zA-Z]|[a-z]{2,3}|\(?[0-9a-z]{1,3}[+\-−×][0-9a-z]{1,3}\)?|[0-9]{1,3}[a-z])$")
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')


def _unescape(t: str) -> str:
    return t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")


def _sup(t: str) -> str:
    return t.translate(_SUP) if _SUP_OK.match(t) else f"^({t})"


def extract_maths_pages(pdf_path: Path) -> list[str]:
    """Each page's text with maths kept: a power printed small and raised ('1' then a small '2') becomes 1², and a
    fraction printed as one number above another, inside a line of working, becomes 1/10 in that line. Plain text
    from a PDF loses both ('12', '104', '9 × 1'). Uses pdftotext's word boxes, block by block, so columns stay apart."""
    out = subprocess.run(["pdftotext", "-enc", "UTF-8", "-bbox-layout", str(pdf_path), "-"], check=True,
                         capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
    pages = []
    for page in out.split("<page ")[1:]:
        blocks = []
        for block in page.split("<block ")[1:]:
            lines = []
            for line in block.split("<line ")[1:]:
                ws = [[float(a), float(b), float(c), float(d), _unescape(t)] for a, b, c, d, t in _WORD.findall(line)]
                lines.append(ws)
            blocks.append(lines)
        # powers: a small raised word right after a normal one, on the same line, joins it
        for lines in blocks:
            for ws in lines:
                i = 1
                while i < len(ws):
                    prev, w = ws[i - 1], ws[i]
                    ph, h = prev[3] - prev[1], w[3] - w[1]
                    if (h < 0.8 * ph and w[0] - prev[2] < 2.0 and prev[3] - w[3] > 0.2 * ph and _EXP_LIKE.match(w[4])
                            and not prev[4].endswith((",", ".", ":", ";"))):
                        prev[4] += _sup(w[4])
                        prev[2] = max(prev[2], w[2])
                        del ws[i]
                        continue
                    i += 1
        # fractions: one short word directly above another (same middle), with working on either side at the
        # height between them
        allw = [(bi, w) for bi, lines in enumerate(blocks) for ws in lines for w in ws]
        drop: set[int] = set()
        moved: list[tuple[int, list]] = []
        for i, (bi, a) in enumerate(allw):
            if id(a) in drop or not _FRAC_PART.match(a[4]):
                continue
            ah, acx = a[3] - a[1], (a[0] + a[2]) / 2
            for bj, b in allw:
                if b is a or id(b) in drop or not _FRAC_PART.match(b[4]):
                    continue
                bh, bcx = b[3] - b[1], (b[0] + b[2]) / 2
                if not (abs(acx - bcx) < max(3.0, 0.35 * max(a[2] - a[0], b[2] - b[0])) and -0.35 * ah < b[1] - a[3] < 0.35 * ah
                        and 0.6 < bh / max(ah, 0.1) < 1.6):
                    continue
                mid = (a[3] + b[1]) / 2
                ctx = [c for _, c in allw if c is not a and c is not b and abs((c[1] + c[3]) / 2 - mid) < 0.3 * ah
                       and (0 < a[0] - c[2] < 40 or 0 < c[0] - max(a[2], b[2]) < 40)]
                if not ctx:
                    continue
                frac = [min(a[0], b[0]), mid - ah / 2, max(a[2], b[2]), mid + ah / 2, f"{a[4]}/{b[4]}"]
                drop.update((id(a), id(b)))
                near = min(ctx, key=lambda c: min(abs(a[0] - c[2]), abs(c[0] - a[2])))
                target = next(k for k, c in allw if c is near)
                moved.append((target, frac))
                break
        text_blocks = []
        for bi, lines in enumerate(blocks):
            ws = [w for ln in lines for w in ln if id(w) not in drop] + [f for t, f in moved if t == bi]
            if not ws:
                continue
            if not any(id(w) in drop for ln in lines for w in ln) and not any(t == bi for t, _ in moved):
                text_blocks.append("\n".join(" ".join(w[4] for w in ln) for ln in lines if ln))
                continue
            hs = sorted(w[3] - w[1] for w in ws)
            tol = 0.5 * hs[len(hs) // 2]
            rows: list[list[list]] = []
            for w in sorted(ws, key=lambda w: (w[1] + w[3]) / 2):
                cy = (w[1] + w[3]) / 2
                if rows and abs(cy - rows[-1][0][5]) < tol:
                    rows[-1].append(w + [rows[-1][0][5]])
                else:
                    rows.append([w + [cy]])
            text_blocks.append("\n".join(" ".join(w[4] for w in sorted(r, key=lambda w: w[0])) for r in rows))
        pages.append("\n".join(text_blocks) + "\n")
    return pages


_BOLD = re.compile(r"<text [^>]*>\s*<b>([^<]{3,70})</b>\s*</text>")
_NOT_TOPIC = re.compile(r"^(figure it out|hint|try this|think|activity|example|solution|note|approach \d|answer|ans\b|"
                        r"summary|learning material|math talk|did you know|let us|exercise|isometric|dot paper|grid)", re.I)


def bold_headings(pdf_path: Path) -> list[str]:
    """Headings printed in bold on their own line ('Triangles', 'Area of any Polygon'), in order: a maths chapter
    that numbers only its first section uses these as its topics. Empty when Poppler's pdftohtml isn't there."""
    if not shutil.which("pdftohtml"):
        return []
    try:
        xml = subprocess.run(["pdftohtml", "-xml", "-i", "-q", "-stdout", str(pdf_path)], check=True, capture_output=True,
                             timeout=300).stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return []
    out = []
    for raw in _BOLD.findall(xml):
        t = " ".join(_unescape(raw).split()).strip()
        if (t[:1].isupper() and not t.isupper() and not t.endswith((".", ",", ":", ";")) and len(t.split()) <= 9
                and not _NOT_TOPIC.match(t) and not re.search(r"\d", t)):
            out.append(t)
    return out


_LABEL_LINE = re.compile(r"^[A-Za-z(][\w()'’-]*(?: [\w()'’-]+){0,2}$")   # 'Electric', 'Electrode 2', '(Liquid)'
_TRAILING_LABEL = re.compile(r"^(?P<body>.*\S)\s+(?P<label>[A-Z][a-z]+(?: \d{1,2}| [A-Z][a-z]+)?)$")


def strip_figure_labels(text: str) -> str:
    """Take a diagram's labels out of the running text. A PDF puts the labels beside a figure ('Electric lamp',
    'Electrode 1', 'Electrolyte (Liquid)') in reading order between the lines of the paragraph next to it, so the text
    read 'a chemical reaction between the Electrode 1 rods and …'. A short line with no full stop that sits inside a
    sentence (the line before doesn't end it and the line after carries on in lower case) is a label and is dropped;
    so is a label glued onto the end of a line (the same word appears as a label on its own on that page, or the
    sentence carries on in lower case after a number label like 'Electrode 1'). Headings are kept: they end the
    sentence before them or start a new one."""
    lines = text.split("\n")
    words = [ln.strip() for ln in lines]
    label_like = {w.split()[0].lower() for w in words if w and len(w) <= 25 and _LABEL_LINE.match(w)
                  and not w.endswith((".", "?", "!", ":", ";", ","))}
    out: list[str] = []
    for i, ln in enumerate(lines):
        w = words[i]
        nxt = next((x for x in words[i + 1:] if x), "")
        prev = next((x for x in reversed(out) if x.strip()), "").strip()
        inside = bool(prev) and len(prev) >= 30 and not prev.endswith((".", "?", "!", ":", ";")) and nxt[:1].islower()
        if w and len(w) <= 25 and len(w.split()) <= 3 and _LABEL_LINE.match(w) and inside \
                and not w.endswith((".", "?", "!", ":", ";", ",")):
            continue  # a label between two halves of a sentence
        if w and len(w) <= 20 and len(w.split()) <= 2 and re.fullmatch(r"[a-z][a-z'’-]*( [a-z][a-z'’-]*)?", w):
            continue  # a stray label word on its own line ('container', 'lamp'): body text never wraps like that
        if inside and re.match(r"(?i)^(fig(ure)?s?\.?|table)\s*\d", w):
            continue  # a figure caption printed between two halves of a sentence
        m = _TRAILING_LABEL.match(w) if w and nxt[:1].islower() else None
        if m and len(m.group("body")) >= 25 and not m.group("body").endswith((".", "?", "!", ":", ";")):
            label = m.group("label")
            if label.split()[0].lower() in label_like - {label.lower()} or re.search(r" \d{1,2}$", label):
                ln = ln[: ln.rfind(label)].rstrip()
        out.append(ln)
    return "\n".join(out)


def repair_split_words(pages: list[str]) -> list[str]:
    """Join words that pypdf splits with a stray space ('lear nt' -> 'learnt') when the joined
    form occurs elsewhere in the book."""
    vocab = Counter(w.lower() for p in pages for w in re.findall(r"[A-Za-z]{3,}", p))
    pair = re.compile(r"\b([A-Za-z]{2,})\s([a-z]{1,6})\b")

    def fix(m: re.Match[str]) -> str:
        joined = m.group(1) + m.group(2)
        if vocab.get(joined.lower(), 0) >= 1 and vocab.get(m.group(2).lower(), 0) <= 3:
            return joined
        return m.group(0)

    return [pair.sub(fix, p) for p in pages]


def _clean_lines(text: str, chapter_title: str) -> Iterator[str]:
    head = re.sub(r"\W+", "", chapter_title).upper()
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or _NOISE.match(stripped):
            continue
        if head and re.sub(r"\W+", "", stripped).upper().startswith(head[:18]) and len(stripped) < 70:
            continue  # running header
        yield stripped


def split_sections(pages: list[str], chapter_no: int, chapter_title: str, first_page: int,
                   last_page: int, page_offset: int, sections: list[str],
                   anchors: dict[str, str] | None = None) -> dict[str, list[tuple[int, str]]]:
    """Group a chapter's lines by numbered section ('8.5'), keeping each line's book page.

    Text before the first heading goes to the first section. Keyword boxes, the end-of-chapter
    summary and exercises are dropped. `anchors` maps a section to the first words of its body,
    for two-column pages where the heading and its text are separated in reading order."""
    heading = re.compile(r"^(%d\.(\d{1,2}))\s+[A-Z(]" % chapter_no)
    wanted = set(sections)
    out: dict[str, list[tuple[int, str]]] = {s: [] for s in sections}
    current = sections[0]
    skipping = False
    for book_page in range(first_page, last_page + 1):
        idx = book_page + page_offset - 1
        if idx < 0 or idx >= len(pages):
            continue
        for line in _clean_lines(pages[idx], chapter_title):
            if _SKIP_FROM.match(line):
                skipping = True
                continue
            m = heading.match(line)
            if m and m.group(1) in wanted:
                current, skipping = m.group(1), False
            for sec, phrase in (anchors or {}).items():
                if line.startswith(phrase):
                    current, skipping = sec, False
            if not skipping:
                out[current].append((book_page, line))
    return out


def chunk_lines(lines: list[tuple[int, str]], target_words: int = 120, max_words: int = 170) -> list[tuple[int, str]]:
    """Merge lines into paragraphs of about `target_words`, splitting at sentence ends."""
    text_parts: list[tuple[int, str]] = []
    for page, line in lines:
        text_parts.append((page, line))
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    start_page = text_parts[0][0] if text_parts else 0
    words = 0
    for page, line in text_parts:
        if not buf:
            start_page = page
        buf.append(line)
        words += len(line.split())
        ends_sentence = line.endswith((".", "?", "!", ":"))
        if (words >= target_words and ends_sentence) or words >= max_words:
            chunks.append((start_page, _join(buf)))
            buf, words = [], 0
    if buf:
        tail = _join(buf)
        if chunks and len(tail.split()) < 40:
            page, prev = chunks[-1]
            chunks[-1] = (page, prev + " " + tail)
        else:
            chunks.append((start_page, tail))
    return [(p, c) for p, c in chunks if len(c.split()) >= 8]


def _join(lines: Iterable[str]) -> str:
    text = " ".join(lines)
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # hyphenated line breaks
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:?!)])", r"\1", text)
    return text.strip()


def build_passages(pages: list[str], chapter: dict, page_offset: int) -> list[Passage]:
    """Produce all passages for one chapter from the syllabus map entry."""
    secs = [t["sec"] for t in chapter["topics"]]
    anchors = {t["sec"]: t["anchor"] for t in chapter["topics"] if t.get("anchor")}
    grouped = split_sections(pages, chapter["no"], chapter["title"], chapter["start_page"],
                             chapter["end_page"], page_offset, secs, anchors)
    passages: list[Passage] = []
    for sec, lines in grouped.items():
        for page, body in chunk_lines(lines):
            passages.append(Passage(sec, page, body))
    return passages


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z]+", "", text.upper())


def find_chapter(pages: list[str], chapters: Sequence[dict]) -> tuple[int, int] | None:
    """(chapter number, index of its first page) for a single-chapter PDF, by the chapter title at the top of one of
    its first three pages (NCERT chapter files sometimes start with a cover or blank page). Longest title wins, so
    'Light' never beats 'Stars and the Solar System'."""
    for idx, page in enumerate(pages[:3]):
        head = _norm(re.sub(r"(?i)\bchapter\b", " ", page[:400]))[:80]
        hits = [(len(_norm(c["title"])), c["no"]) for c in chapters if _norm(c["title"]) and _norm(c["title"]) in head]
        if hits:
            return max(hits)[1], idx
    return None


def numbered_like(pages: list[str], first: int, chapter: dict, share: float = 0.6) -> bool:
    """True when the chapter file's pages carry the chapter's printed page numbers (its start page onwards) on most
    pages. A chapter with the same title from another book or edition (NCERT's newer 'Curiosity' book has 'Light:
    Mirrors and Lenses') has different page numbers, so its title alone no longer makes it a match."""
    start, end = chapter.get("start_page"), chapter.get("end_page")
    if not start or not end:
        return True
    span = end - start + 1
    got = pages[first:first + span]
    if len(got) < span * share:
        return False
    hits = sum(1 for i, text in enumerate(got) if re.search(rf"(?<!\d){start + i}(?!\d)", text or ""))
    return hits >= max(2, round(span * share))


@dataclass(frozen=True, slots=True)
class Book:
    """The textbook's pages laid out as in the printed book (empty strings for chapters not supplied). `rejected` are
    files whose title matched a chapter but whose page numbers did not (another book or edition): (file, chapter)."""

    pages: list[str]
    chapters: tuple[int, ...]
    unmatched: tuple[str, ...]
    rejected: tuple[tuple[str, int], ...] = ()
    removed: int = 0  # passages an earlier version had taken from a rejected file, now deleted
    removed_chapters: tuple[int, ...] = ()  # the chapters those passages belonged to


def assemble_book(paths: Sequence[Path], chapters: Sequence[dict], page_offset: int,
                  extract=extract_pages) -> Book:
    """Read the textbook from one whole-book PDF or from several chapter PDFs (any names, any order).

    A file at least as long as the book is used as the whole book. Each shorter file is matched to its chapter by
    the title on its first page and placed at that chapter's pages, so passages keep their printed page numbers."""
    total = max(c["end_page"] for c in chapters) + page_offset
    files = [extract(Path(p)) for p in paths]
    for pages in files:
        if len(pages) >= total - 2:
            return Book(pages, tuple(c["no"] for c in chapters), ())
    book = [""] * total
    by_no = {c["no"]: c for c in chapters}
    found: list[int] = []
    unmatched: list[str] = []
    rejected: list[tuple[str, int]] = []
    for path, pages in zip(paths, files):
        hit = find_chapter(pages, chapters)
        if not hit:
            unmatched.append(Path(path).name)
            continue
        no, first = hit
        ch = by_no[no]
        if not numbered_like(pages, first, ch):
            unmatched.append(Path(path).name)
            rejected.append((Path(path).name, no))
            continue
        span = ch["end_page"] - ch["start_page"] + 1
        for i, text in enumerate(pages[first:first + span]):
            book[ch["start_page"] + page_offset - 1 + i] = text
        found.append(no)
    return Book(book, tuple(sorted(set(found))), tuple(unmatched), tuple(rejected))
