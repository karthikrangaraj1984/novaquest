"""Find a book's chapters and topics from its text (SY-5): the offline method, also the safety net when Claude is
unavailable. Pure functions only, so they are easy to test with any book.

A "line" is (page, text), with page counted from 1 within the uploaded file (or the whole book for one PDF)."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

Line = tuple[int, str]

_CHAPTER = re.compile(r"^\s*(?:chapter|unit|lesson)\s*[-:.]?\s*(\d{1,2}|[ivxl]{1,6})\b\s*[-:.–—]?\s*(.*)$", re.I)
_NUMBERED = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?\s+([A-Z][^\n]{2,70})$")
_NOT_HEADING_START = re.compile(r"^(activity|fig\.?|figure|table|example|box|note)\b", re.I)
_ASKS = re.compile(r"^(what|why|how|where|when|which|who|do|does|is|are|can)\b", re.I)
_CAPS = re.compile(r"^[A-Z][A-Z0-9 ,:'()&/\-–—]{3,70}$")
_END_HEAD = re.compile(r"^(exercises?|keywords?|key words|summary|what you have learnt|extended learning|check your progress|"
                       r"activities and projects|points to remember|glossary|index|snapshots|keep the curiosity alive|"
                       r"discover, design,? and debate)\b", re.I)


def end_heading(text: str) -> bool:
    """An end-of-chapter heading ('Exercises', 'Snapshots', 'Keep the curiosity alive'): short, capitalised, no full
    stop, so a sentence that merely starts with such a word ('Index (AQI) helps us...', 'exercise for the next few
    days.') no longer ends the chapter's topics early."""
    t = text.strip()
    return bool(_END_HEAD.match(t)) and t[:1].isupper() and not t.endswith((".", ",", ";")) and len(t.split()) <= 6
_STOP_HEAD = re.compile(r"^(exercises?|questions|keywords?|key words|summary|what you have learnt|extended learning|check your progress|"
                        r"activities and projects|let us recall|points to remember|glossary|contents|index|did you know\??)\b", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARK = re.compile(r"^[>»›]+\s*")  # a heading's arrow icon, as text recognition reads it ('>» 1.1 Photosynthesis')
_PAGE_NOISE = re.compile(r"^\s*(\d{1,4}|[ivxlc]{1,6}|reprint.*|\d{4}-\d{2,4})\s*$", re.I)
_STOP = set("""the a an of and or to in is are it that this on for with as by be can do does what why how which from at we
you i they their there these those was were has have had will would should could not no but if then than so such into
about also more most other some any each many much very our your its his her them him she he who whom when where while
use used using one two three first second also may might must shall upon over under between""".split())
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12,
         "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20}


@dataclass
class Chapter:
    """A detected chapter: its number, title and lines."""

    no: int
    title: str
    lines: list[Line] = field(default_factory=list)
    source: str = ""
    title_from: str = ""  # "contents" when the title came from the book's own contents page
    headings: tuple[str, ...] = ()  # bold headings printed in the chapter (maths books), used when sections aren't numbered


@dataclass
class Topic:
    """A detected topic: title, the index of its first line in the chapter, outcome and key terms."""

    title: str
    start: int
    outcome: str = ""
    terms: list[str] = field(default_factory=list)


def clean_lines(pages: list[str]) -> list[Line]:
    """Non-empty, trimmed lines with their page number; page numbers and running noise removed."""
    out: list[Line] = []
    for i, page in enumerate(pages, 1):
        for raw in page.split("\n"):
            line = _MARK.sub("", " ".join(_CONTROL.sub(" ", raw).split()))  # PDFs can carry control characters (\x07)
            if line and not _PAGE_NOISE.match(line):
                out.append((i, line))
    return out


def _num(token: str) -> int | None:
    t = token.lower()
    return int(t) if t.isdigit() else ROMAN.get(t)


def _title_after(lines: list[Line], i: int, rest: str) -> str:
    """A chapter title: the rest of the 'Chapter N' line, else the next short line."""
    rest = rest.strip(" :-–—.")
    if len(rest) >= 3:
        return tidy_title(rest)
    for _, text in lines[i + 1:i + 4]:
        if 3 <= len(text) <= 80 and not _PAGE_NOISE.match(text):
            return tidy_title(text)
    return ""


def tidy_title(text: str) -> str:
    """'MICROORGANISMS : FRIEND AND FOE' -> 'Microorganisms: Friend and Foe' (keeps mixed-case titles as they are)."""
    text = re.sub(r"\s+([:;,?])", r"\1", " ".join(text.split())).strip(" .")
    text = re.sub(r"(?<=[^\W\d_])- (?=[^\W\d_])", lambda m: "-" if text[m.end()].isupper() else m.group(0), text)  # 'Baudhāyana- Pythagoras'
    if text.isupper():
        small = {"and", "or", "of", "the", "in", "on", "a", "an", "to", "for", "with", "at", "by"}
        words = text.lower().split()
        text = " ".join(w if (j and w in small) else "-".join(p[:1].upper() + p[1:] for p in w.split("-"))
                        for j, w in enumerate(words))
    return text[:120]


def chapter_heading(lines: list[Line], first_lines: int = 12) -> tuple[int | None, str]:
    """(number, title) from a 'Chapter N …' line near the top of a chapter file, if there is one."""
    for i, (_, text) in enumerate(lines[:first_lines]):
        m = _CHAPTER.match(text)
        if m and _num(m.group(1)):
            return _num(m.group(1)), _title_after(lines, i, m.group(2))
    return None, ""


def split_chapters(lines: list[Line]) -> list[Chapter]:
    """Split one whole-book PDF at its 'Chapter N' / 'Unit N' headings (in increasing order). No headings: one chapter."""
    starts: list[tuple[int, int, str]] = []
    expect = 1
    for i, (_, text) in enumerate(lines):
        m = _CHAPTER.match(text)
        n = _num(m.group(1)) if m else None
        if n is not None and n == expect:  # skips the contents page's list only if it repeats; keeps the body's order
            starts.append((i, n, _title_after(lines, i, m.group(2))))
            expect += 1
    if len(starts) >= 2:
        # a contents page lists 'Chapter 1 … Chapter N' close together: if the run restarts later, use the later run
        later = [j for j, (i, n, _) in enumerate(starts) if n == 1]
        starts = starts[later[-1]:] if later else starts
    if not starts:
        return [Chapter(1, "", list(lines))]
    out = []
    for k, (i, n, title) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        out.append(Chapter(n, title, lines[i:end]))
    return out


def _running_title(lines: list[Line], no: int) -> str:
    """A chapter's title from its running header 'N Title', printed on many of its pages ('3 Forces and energy');
    stray marks text recognition adds at the end ('… energy 2', '… energy >') are left off."""
    pages: dict[str, set[int]] = {}
    for page, text in lines:
        m = re.match(rf"^{no}\s+([A-Z][A-Za-z][^.?!]{{2,70}})$", text)
        if m:
            title = re.sub(r"(\s+([^\w\s]+|\d|[A-Z]))+$", "", m.group(1)).strip(" .:|")
            if len(title) >= 3:
                pages.setdefault(title, set()).add(page)
    best = max(pages.items(), key=lambda kv: len(kv[1]), default=("", set()))
    return tidy_title(best[0]) if len(best[1]) >= 3 else ""


def _opener_title(lines: list[Line], at: int) -> str:
    """The title above a chapter's first numbered section on its opening page ('Properties of' / 'materials')."""
    page = lines[at][0]
    above = [t for p, t in lines[max(0, at - 6):at] if p == page]
    above = [t for t in above if 2 <= len(t) <= 40 and not re.search(r"\d", t) and t[:1].isalpha()][-3:]
    return tidy_title(" ".join(above)) if above else ""


def split_sections(lines: list[Line]) -> list[Chapter]:
    """Split a whole-book PDF without 'Chapter N' headings at its numbered sections ('1.1 Photosynthesis', '2.1 Atomic
    structure', as Cambridge books print them): chapter N starts on the page of its first section 'N.1', in order.
    Contents pages are skipped. Empty when the book isn't numbered that way (fewer than two chapters)."""
    contents = {p for p, t in lines if re.fullmatch(r"contents", t, re.I)}
    starts: list[int] = []
    for i, (page, text) in enumerate(lines):
        m = _NUMBERED.match(text)
        if (page not in contents and m and int(m.group(1)) == len(starts) + 1 and int(m.group(2)) == 1
                and (not starts or page > lines[starts[-1]][0])):
            starts.append(i)
    if len(starts) < 2:
        return []
    begins = [next(j for j in range(i, -1, -1) if j == 0 or lines[j - 1][0] != lines[i][0]) for i in starts]
    out = []
    for k, b in enumerate(begins):
        end = begins[k + 1] if k + 1 < len(begins) else len(lines)
        part = lines[b:end]
        no = k + 1
        out.append(Chapter(no, _running_title(part, no) or _opener_title(lines, starts[k]), part))
    return out


# What text recognition glues onto a section heading from the box beside it ('Transpiration inthis topic youwils',
# 'Conservation of energy Finthitopieyouw'), and numbered questions at a chapter's end ('2.5 The diagram shows …').
_TOPIC_TAIL = re.compile(r"\s+(f?in\s?th\w*\s?to\w*|in this topic).*$", re.I)
_QUESTION_ITEM = re.compile(r"^(the (diagram|graph|table|photograph|picture|chart|map)s?\b|(describe|explain|choose|copy|"
                            r"complete|draw|suggest|write|name|state|calculate|look|list|match|use|predict|compare)\b)", re.I)


_SMALL = {"a", "an", "and", "or", "of", "the", "in", "on", "to", "for", "with", "at", "by", "from", "through", "into", "is", "are"}


def _title_case(text: str) -> bool:
    words = text.split()
    return 1 <= len(words) <= 6 and not text.endswith((".", "?", "!", ";")) and \
        all(w[:1].isupper() or w.lower() in _SMALL or not w[:1].isalpha() for w in words)


def _numbered_prefix(lines: list[Line]) -> int | None:
    """The chapter number its numbered sections use ('5.1 Coal', '5.2 Petroleum' -> 5), if any."""
    counts = Counter(int(m.group(1)) for _, t in lines if (m := _NUMBERED.match(t)))
    if not counts:
        return None
    n, c = counts.most_common(1)[0]
    return n if c >= 2 else None


_QR_CHAPTER = re.compile(r"\b\d{4}CH(\d{2})\b")  # NCERT's QR code label on a chapter's first page: 0877CH02
_FRONT = re.compile(r"^\s*(foreword|preface|contents|about the book|a note for the teachers?|textbook development committee)\b", re.I)


def big_number(pages: list[str]) -> int | None:
    """The chapter number printed large on a chapter's first page, when there is no 'Chapter N' heading: NCERT's QR
    label (0877CH02 -> 2), else a lone number among the first lines of the first page."""
    head = "\n".join(pages[:2])
    m = _QR_CHAPTER.search(head)
    if m and 0 < int(m.group(1)) < 60:
        return int(m.group(1))
    for raw in (pages[0].split("\n")[:12] if pages else []):
        t = raw.strip()
        if t.isdigit() and 0 < int(t) < 60:
            return int(t)
    return None


def is_front_matter(name: str, pages: list[str]) -> bool:
    """A book's front matter file (cover, foreword, contents), not a chapter: NCERT names it like hecu1ps.pdf."""
    if re.search(r"\dps$", Path(name).stem.lower()):
        return True
    top = [" ".join(raw.split()) for page in pages[:4] for raw in page.split("\n")[:8]]
    return sum(1 for t in top if _FRONT.match(t)) >= 2


def _wrapped_title(lines: list[Line]) -> str:
    """The chapter title from the top of a chapter file, joining a title printed over several lines
    ('The Invisible Living' / 'World: Beyond Our' / 'Naked Eye')."""
    for i, (_, t) in enumerate(lines[:6]):
        if 3 <= len(t) <= 80 and not _CHAPTER.match(t):
            parts = [t]
            for _, nxt in lines[i + 1:i + 5]:
                prev = parts[-1]
                joined = " ".join(parts + [nxt])
                caps = prev.isupper() and nxt.isupper() and len(joined) <= 70  # 'A STORY OF' / 'NUMBERS'
                if not caps and (len(prev) < 12 or prev.endswith((".", "?", "!")) or len(joined) > 110 or nxt[:1].isdigit()
                        or _NUMBERED.match(nxt) or _NOT_HEADING_START.match(nxt)
                        or not _title_case(nxt.replace(":", " ").replace("–", " "))):
                    break
                parts.append(nxt)
            return tidy_title(" ".join(parts))
    return ""


def contents_titles(pages: list[str]) -> dict[int, str]:
    """Chapter titles from a book's contents page ('Chapter 3' / 'Health: The Ultimate Treasure' / '28'), keyed by
    chapter number. Empty unless there is a contents page with at least two chapters."""
    lines = clean_lines(pages)
    start = next((k for k, (_, t) in enumerate(lines) if re.match(r"^contents\b", t, re.I)), None)
    if start is None:
        return {}
    lines = lines[start:]  # 'About the Book' before it describes the chapters in sentences: not titles
    out: dict[int, str] = {}
    for i, (_, t) in enumerate(lines):
        m = _CHAPTER.match(t)
        n = _num(m.group(1)) if m else None
        if not n or n in out:
            continue
        rest = re.sub(r"\s+\d{1,3}$", "", m.group(2)).strip(" :–—.")  # a page number, not 'Reasoning-1'
        if len(rest) < 3 and i + 1 < len(lines):
            rest = re.sub(r"\s+\d{1,3}$", "", lines[i + 1][1]).strip(" :–—.")
        if len(rest) >= 3 and not _CHAPTER.match(rest) and len(rest.split()) <= 12 and not re.search(r"[‘’\"“”]", rest):
            out[n] = tidy_title(rest)
    return out if len(out) >= 2 else {}


def number_title(pages: list[str], no: int) -> str:
    """The title printed just before the big chapter number on a chapter's first page, joining up to three lines
    ('Health:' / 'The Ultimate Treasure' / '3'). Text in pictures above it (a bulletin board's headlines) is not
    picked up because a lower-case, numbered or sentence line ends the title."""
    raw = [" ".join(_CONTROL.sub(" ", r).split()) for r in (pages[0].split("\n") if pages else [])]
    raw = [r for r in raw if r]
    at = next((k for k, r in enumerate(raw) if r == str(no)), None)
    if not at:
        return ""
    block = [raw[at - 1]]
    for j in range(at - 2, max(-1, at - 4), -1):
        line = raw[j]
        lower_ok = (line[:1].islower() and len(line) >= 8 and line.split()[0] in _SMALL  # 'of Solutes, Solvents,'
                    and bool(re.search(r"\b[A-Z]", line)))  # title case, unlike a picture's 'of oil and sugar in diet'
        if (len(line) > 40 or not (line[:1].isupper() or lower_ok) or re.search(r"\d", line) or line.endswith((".", "?", "!"))
                or len(" ".join([line] + block)) > 100):
            break
        block.insert(0, line)
    title = tidy_title(" ".join(block))
    return title if len(title) >= 4 and not re.search(r"\d", title) else ""


def file_chapters(files: list[tuple[str, list[str]]]) -> list[Chapter]:
    """One chapter per uploaded file (the usual way boards publish books), numbered by 'Chapter N' (or the big chapter
    number on its first page) when every file has one, else in file-name order (natural sort). Front-matter files
    (cover, foreword, contents) are left out when there are chapter files too."""
    sets: list[dict[int, str]] = []
    if len(files) > 1:
        front = [(n, p) for n, p in files if is_front_matter(n, p)]
        sets = [c for c in (contents_titles(p) for _, p in front) if c]
        files = [(n, p) for n, p in files if (n, p) not in front] or files
    chapters, guessed, first_pages = [], set(), {}
    for name, pages in files:
        lines = clean_lines(pages)
        no, title = chapter_heading(lines)
        no = no or big_number(pages) or _numbered_prefix(lines)
        if not title:
            title = _wrapped_title(lines)
            guessed.add(name)
        first_pages[name] = pages
        chapters.append(Chapter(no or 0, title or tidy_title(Path(name).stem.replace("_", " ")), lines, name))
    nums = [c.no for c in chapters]
    if all(nums) and len(set(nums)) == len(nums):
        chapters.sort(key=lambda c: c.no)
    else:
        chapters.sort(key=lambda c: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", c.source.lower())])
        for i, c in enumerate(chapters, 1):
            c.no = i
    # The book's contents page gives the surest title, but only for the chapter it belongs to: each title is matched to
    # the file whose first page prints it (a two-part book, or one part uploaded with the other part's front matter,
    # has two 'Chapter 1's). By number only when that contents page matches most chapters by their words.
    titles = [t for c in sets for t in c.values()]
    matched: set[str] = set()
    for c in chapters:
        head = f" {_plain(' '.join(_title_block(c.lines)) + ' ' + c.title)} "
        hits = [t for t in titles if len(_plain(t)) >= 4 and f" {_plain(t)} " in head]  # whole words: not 'squ-are a-nd'
        joined = head.replace(" ", "")  # a title printed without its hyphen: 'BAUDHĀYANAPYTHAGORAS' (long titles only)
        hits += [t for t in titles if len(_plain(t)) >= 12 and _plain(t).replace(" ", "") in joined and t not in hits]
        if hits:
            c.title, c.title_from = max(hits, key=lambda t: len(_plain(t))), "contents"
            matched.add(c.source)
    for cset in sets:
        agree = sum(1 for c in chapters if c.source in matched and cset.get(c.no) == c.title)
        if agree * 2 < len([c for c in chapters if cset.get(c.no)]):
            continue  # this contents page is for other chapters (another part of the book): don't number-match it
        for c in chapters:
            if c.source not in matched and cset.get(c.no):
                c.title, c.title_from = cset[c.no], "contents"
                matched.add(c.source)
    for c in chapters:
        if c.source not in matched and c.source in guessed:  # else the title beside the big number
            c.title = number_title(first_pages[c.source], c.no) or c.title
    return chapters


def _title_block(lines: list[Line]) -> list[str]:
    """The short lines at the top of a chapter's first page (number, title, maybe 'Chapter 3'), before its text."""
    out = []
    for _, t in lines[:8]:
        if len(t) > 45 or _NUMBERED.match(t):
            break
        out.append(t)
    return out


def _plain(text: str) -> str:
    """'The BAUDHĀYANA- PYTHAGORAS Theorem' -> 'the baudhayana pythagoras theorem': for matching a title on a page."""
    t = unicodedata.normalize("NFKD", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in t if not unicodedata.combining(ch)).lower()).split())


def _is_caps_heading(text: str, chapter_title: str) -> bool:
    """A short ALL-CAPS heading made of real words (not a drop-cap fragment, an acronym or the running header)."""
    if not _CAPS.match(text) or re.sub(r"\W", "", text).upper() == re.sub(r"\W", "", chapter_title).upper():
        return False
    words = re.findall(r"[A-Z]+", text)
    if not words or any(len(w) <= 2 and w.lower() not in _SMALL for w in words):
        return False
    return len(words) >= 2 or len(words[0]) >= 5


def has_numbered_sections(ch: Chapter) -> bool:
    """True when the chapter numbers its own sections ('3.1 …', '3.2 …'): those are then its topics, as printed."""
    ks = {m.group(2) for _, t in ch.lines if (m := _NUMBERED.match(t)) and ch.no and int(m.group(1)) == ch.no}
    return len(ks) >= 2


_SECTION_NO = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){1,3}\.?\s*")


def strip_section_number(title: str) -> tuple[str, int]:
    """('3.2.2 Keep the environment clean' -> ('Keep the environment clean', 3): the title without its printed number,
    and how many levels that number had (0 when there was none)."""
    m = _SECTION_NO.match(title)
    if not m:
        return title.strip(), 0
    return title[m.end():].strip(), m.group(0).strip().rstrip(".").count(".") + 1


def find_topics(ch: Chapter, max_topics: int = 14) -> list[Topic]:
    """Topics from the chapter's own headings: numbered sections ('2.3 Useful microorganisms') first, else short
    ALL-CAPS headings, else the whole chapter as one topic. End-of-chapter exercises are never a topic."""
    lines = ch.lines
    numbered: list[Topic] = []
    seen: set[str] = set()
    prefix = ch.no if ch.no and any((m := _NUMBERED.match(t)) and int(m.group(1)) == ch.no for _, t in lines) else _numbered_prefix(lines)
    last = 0
    stop_at = next((i for i, (_, t) in enumerate(lines) if i > 20 and end_heading(t)), len(lines))
    for i, (_, text) in enumerate(lines):
        m = _NUMBERED.match(text)
        if not m or int(m.group(1)) != prefix or m.group(2) in seen or _STOP_HEAD.match(m.group(3)) or int(m.group(2)) < last:
            continue
        if i >= stop_at and int(m.group(2)) != last + 1:  # after an end heading only the very next section counts:
            continue                                      # books with a 'Summary' after every section go on to the next
        title = _TOPIC_TAIL.sub("", m.group(3)).strip()
        if _QUESTION_ITEM.match(title):  # '2.5 The diagram shows …': a numbered question at the chapter's end
            continue
        nxt = lines[i + 1][1] if i + 1 < len(lines) else ""
        words = title.split()
        unfinished = (words[-1].lower() in _SMALL | {"be", "its", "their", "our", "your"} or nxt[:1].islower()
                      or (_ASKS.match(title) and not title.endswith("?")) or (len(words) >= 3 and len(nxt.split()) == 1
                                                                               and nxt.lower() not in title.lower()))
        if nxt and unfinished and not title.endswith(("?", ":", ".")) and len(nxt) <= 45 and not _NUMBERED.match(nxt) and not _NOT_HEADING_START.match(nxt) \
                and not nxt.endswith("."):
            title = _TOPIC_TAIL.sub("", f"{title} {nxt}").strip()  # a heading wrapped onto a second line
        seen.add(m.group(2))
        last = int(m.group(2))
        printed = Counter(_TOPIC_TAIL.sub("", f.group(3)).strip() for _, t in lines[i:] if (f := _NUMBERED.match(t))
                          and f.group(1) == m.group(1) and f.group(2) == m.group(2))
        fuller = [v for v in printed if v.startswith(title.rstrip(" .:"))]
        if fuller:  # the title as most often printed (running headers), the longest of equals:
            title = max(fuller, key=lambda v: (printed[v], len(v)))  # '2.4 Simple and giant' -> '… giant structures'
        numbered.append(Topic(tidy_title(title), i))
    if len(numbered) < 2 and ch.headings:  # a maths chapter that numbers only its first section: its bold headings
        want = {" ".join(h.split()).lower() for h in ch.headings}
        bold = [Topic(tidy_title(text), i) for i, (_, text) in enumerate(lines[:stop_at])
                if i > 0 and text.lower() in want and not _STOP_HEAD.match(text)]
        merged: list[Topic] = []
        for t in sorted(numbered + bold, key=lambda t: t.start):
            if not merged or (t.title.lower() != merged[-1].title.lower() and t.start - merged[-1].start >= 8):
                merged.append(t)
        if len(merged) >= 2:
            numbered = merged
    if len(numbered) >= 2:
        topics = numbered
    else:
        repeats = Counter(t for _, t in lines)
        caps = [Topic(tidy_title(text), i) for i, (_, text) in enumerate(lines[:stop_at])
                if i > 0 and repeats[text] < 3 and _is_caps_heading(text, ch.title) and not _STOP_HEAD.match(text)
                and len(text) <= 60]
        dedup: list[Topic] = []
        for t in caps:
            if not dedup or (t.title != dedup[-1].title and t.start - dedup[-1].start >= 3):
                dedup.append(t)
        topics = dedup if 2 <= len(dedup) <= max_topics * 2 else [Topic(ch.title or "The whole chapter", 0)]
    if len(topics) > max_topics:  # too many tiny headings: keep the most spread-out ones
        step = len(topics) / max_topics
        topics = [topics[int(k * step)] for k in range(max_topics)]
    if topics[0].start > 0 and topics[0].start < 40:
        topics[0] = Topic(topics[0].title, 0)  # the chapter's opening text belongs to its first topic
    for k, t in enumerate(topics):
        end = topics[k + 1].start if k + 1 < len(topics) else len(lines)
        body = " ".join(text for _, text in lines[t.start:end])
        t.terms = key_terms(body, t.title)
        t.outcome = t.outcome or f"Explain the main ideas of {t.title[:1].lower() + t.title[1:]}."
    return topics


def key_terms(text: str, title: str = "", n: int = 5) -> list[str]:
    """The most frequent meaningful words of a topic (used to spot on-topic answers and as distractors)."""
    words = [w for w in re.findall(r"[a-z][a-z\-]{3,}", text.lower()) if w not in _STOP]
    counts = Counter(words)
    for w in re.findall(r"[a-z][a-z\-]{3,}", title.lower()):
        if w not in _STOP:
            counts[w] += 3
    return [w for w, _ in counts.most_common(n)]


def topic_lines(ch: Chapter, topics: list[Topic]) -> list[list[Line]]:
    """The lines of each topic, with end-of-chapter exercises, summaries and keyword boxes left out."""
    out = []
    for k, t in enumerate(topics):
        end = topics[k + 1].start if k + 1 < len(topics) else len(ch.lines)
        keep, skipping = [], False
        for page, text in ch.lines[t.start:end]:
            if end_heading(text):
                skipping = True
                continue
            if not skipping:
                keep.append((page, text))
        out.append(keep)
    return out
