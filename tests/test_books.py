"""School books (SY-5, SY-6): a school uploads its own textbook for any board, subject and grade; NovaQuest reads it,
the admin reviews and publishes it, classes study one or more books, and each school's books stay private."""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
from pathlib import Path

from novaquest.ai.offline import OfflineEngine
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import books, ingest, institutions, ocr, learn, outline, path, pictures, questions, search, syllabus, teach
from tests.base import PASSWORD, ROOT, NQTestCase

PDF = ROOT / "data" / "source" / "ncert_science_class8_2018.pdf"
MAP = json.loads((ROOT / "data" / "syllabus" / "ncert_g8_science.json").read_text(encoding="utf-8"))


def chapter_pdfs(folder: Path, nos: tuple[int, ...]) -> list[Path]:
    """Real textbook chapters as separate files, like a board's website offers them."""
    import pypdf

    reader = pypdf.PdfReader(str(PDF))
    off = MAP["pdf_page_offset"]
    out = []
    for no in nos:
        ch = MAP["chapters"][no - 1]
        w = pypdf.PdfWriter()
        for i in range(ch["start_page"] + off - 1, ch["end_page"] + off):
            w.add_page(reader.pages[i])
        p = folder / f"science-chapter-{no}.pdf"
        with open(p, "wb") as f:
            w.write(f)
        out.append(p)
    return out


def made_up_book(path: Path) -> Path:
    """A one-file book in another style: 'Chapter N' headings and ALL-CAPS section headings, no numbering."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    text = {
        (1, "Motion and Measurement"): {
            "DISTANCE AND DISPLACEMENT": "Distance is the total length of the path an object travels. Displacement is the "
            "shortest distance from the start to the end point, with a direction. A runner who goes once round a circular "
            "track covers a distance of four hundred metres but has zero displacement.",
            "SPEED AND VELOCITY": "Speed tells us how fast an object moves. Speed is distance divided by time. Velocity is "
            "speed in a given direction. A car moving at sixty kilometres per hour towards the north has a velocity.",
            "UNIFORM MOTION": "When an object covers equal distances in equal intervals of time, its motion is uniform. "
            "The hands of a clock move with uniform motion. A bus in city traffic does not."},
        (2, "Simple Machines"): {
            "LEVERS": "A lever is a rigid bar that turns about a fixed point called the fulcrum. A seesaw, a pair of "
            "scissors and a bottle opener are levers. Levers help us lift heavy loads with a small effort.",
            "PULLEYS": "A pulley is a wheel with a groove for a rope. A single fixed pulley changes the direction of the "
            "force. We use pulleys to draw water from a well and to raise a flag.",
            "INCLINED PLANES": "An inclined plane is a slope. It lets us raise a load using less force over a longer "
            "distance. Ramps for wheelchairs are inclined planes."}}
    c = canvas.Canvas(str(path), pagesize=A4)
    for (no, title), sections in text.items():
        y = 790
        c.setFont("Helvetica-Bold", 18)
        c.drawString(60, y, f"Chapter {no}: {title}")
        y -= 40
        for head, body in sections.items():
            c.setFont("Helvetica-Bold", 12)
            c.drawString(60, y, head)
            y -= 20
            c.setFont("Helvetica", 11)
            words, line = body.split(), ""
            for w in words * 2:  # enough text per section to make passages
                if len(line) + len(w) > 85:
                    c.drawString(60, y, line)
                    y -= 15
                    line = ""
                    if y < 60:
                        c.showPage()
                        y = 790
                        c.setFont("Helvetica", 11)
                line += w + " "
            c.drawString(60, y, line)
            y -= 30
        c.showPage()
    c.save()
    return path


def scanned_copy(src: Path, dest: Path, dpi: int = 110) -> Path:
    """`src` as a scanned book: every page a picture, no text layer (as a scanner or phone app makes it)."""
    import subprocess

    from PIL import Image

    base = dest.with_suffix("")
    subprocess.run(["pdftoppm", "-r", str(dpi), "-jpeg", str(src), str(base)], check=True)
    shots = sorted(dest.parent.glob(base.name + "-*.jpg"))
    imgs = [Image.open(f).convert("RGB") for f in shots]
    imgs[0].save(dest, "PDF", resolution=dpi, save_all=True, append_images=imgs[1:])
    for f in shots:
        f.unlink()
    return dest


class FakeClaude:
    """Stands in for the Claude engine: outlines chapters and drafts topic material from fixed answers."""

    name = "claude"

    def __init__(self, bad_codes: tuple[str, ...] = ()):
        self.offline = OfflineEngine()
        self.bad_codes = bad_codes
        self.drafted: list[str] = []

    def outline_chapter(self, course, chapter_title, lines):
        starts = [i for i, t in enumerate(lines) if t.isupper() and len(t) > 5][:3] or [0]
        return [{"title": lines[s].title(), "start": s, "outcome": f"Describe {lines[s].lower()}.", "terms": [],
                 "chapter_title": chapter_title} for s in starts]

    def draft_topic(self, topic):
        self.drafted.append(topic.code)
        cards = [{"title": f"Card {i}", "say": [f"{topic.title} in simple words, part {i}."],
                  "pic": {"kind": "icons", "items": [{"icon": "🔧", "text": "First idea"}, {"icon": "⚙️", "text": "Second idea"}]}}
                 for i in range(1, 4)]
        if topic.code in self.bad_codes:
            cards[0]["pic"] = {"kind": "scene", "scene": "made-up", "labels": []}  # must be rejected
        return {"pictures": {"crux": f"The big idea of {topic.title}.", "cards": cards,
                             "crux_points": ["Point one.", "Point two."],
                             "quiz": [{"q": "Which is right?", "options": ["This", "That"], "answer": 0, "why": "Because."},
                                      {"q": "And now?", "options": ["Yes", "No"], "answer": 1, "why": "Because."}]},
                "lesson": {"explain": f"{topic.title} explained simply.", "explain_again": "Another way.", "example": None,
                           "checks": [{"q": "Pick one", "options": ["A", "B", "C", "D"], "answer": 2, "why": "C is right."}]},
                "mixups": [{"label": "A wrong idea", "nova_says": "I think levers make work disappear, right?",
                            "correct_idea": "Levers make work easier; they do not remove it.",
                            "key_ideas": [["easier", "less effort"], ["fulcrum", "turns"]],
                            "wrong_signals": ["work disappears"], "probe": "Where does the effort go?",
                            "example_prompt": "Give me an example of a lever."},
                           {"label": "", "nova_says": "broken"}]}  # the second mix-up is invalid and dropped

    def __getattr__(self, name):  # everything else as offline
        return getattr(self.offline, name)


class SchoolBooks(NQTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-books-"))
        self.storage = self.tmp / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def upload(self, school, files: list[Path], **kw) -> int:
        handles = [(p.name, open(p, "rb")) for p in files]
        try:
            return books.create(self.db, school.admin, board=kw.get("board", "CBSE"), subject=kw.get("subject", "Science"),
                                grade=kw.get("grade", "8"), title=kw.get("title", "Our Science Book"), files=handles,
                                storage=self.storage)
        finally:
            for _, h in handles:
                h.close()

    def read(self, school, sid, engine=None):
        books.read_book(self.db, engine or OfflineEngine(), school.inst, sid, self.storage)
        with self.db.tenant(school.inst) as tx:
            return books.detail(tx, sid)

    def test_school_uploads_chapter_pdfs_reviews_and_publishes(self):
        """[SY-5] A school admin uploads a book as chapter PDFs (any names) for a board, subject and grade; NovaQuest
        reads it in the background, finds each chapter and its topics from the book's own headings, cuts the text into
        cited passages, and keeps it hidden until the admin has reviewed (renamed or removed topics) and published it."""
        if not PDF.exists():
            self.skipTest("textbook PDF not present")
        school = self.make_school()
        files = chapter_pdfs(self.tmp, (8, 2))
        sid = self.upload(school, files)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(syllabus.get_syllabus(tx, sid)["status"], "processing")
            self.assertEqual(tx.val("SELECT count(*) FROM jobs WHERE kind='book_read'"), 1)
            self.assertNotIn(sid, [b["id"] for b in syllabus.available_books(tx)], "not offered to classes yet")
        d = self.read(school, sid)
        self.assertEqual(d["book"]["status"], "review")
        self.assertEqual([c["code"] for c in d["tree"]], ["2", "8"], "chapters in book order, numbered as in the book")
        titles = [t["title"] for c in d["tree"] for t in c["topics"]]
        for expected in ("Microorganisms and Us", "Food Preservation", "Parts of the Cell"):
            self.assertIn(expected, titles)
        self.assertTrue(all(d["counts"].get(t["id"]) for c in d["tree"] for t in c["topics"][:3]))
        with self.db.tenant(school.inst) as tx:
            ref = tx.val("SELECT ref FROM source_passages WHERE syllabus_id=%s ORDER BY id LIMIT 1", [sid])
            self.assertRegex(ref, r"^Our Science Book, Ch 2, p\. \d+$")
            first = d["tree"][0]["topics"][0]
            books.rename(tx, school.admin, sid, first["id"], "Tiny living things", "Name the main groups of microorganisms.")
            moved = tx.val("SELECT count(*) FROM source_passages WHERE node_id=%s", [d["tree"][0]["topics"][1]["id"]])
            books.remove_topic(tx, school.admin, sid, d["tree"][0]["topics"][1]["id"])
            self.assertEqual(tx.val("SELECT count(*) FROM source_passages WHERE node_id=%s", [first["id"]]),
                             d["counts"][first["id"]] + moved, "a removed topic's text joins its neighbour")
            with self.assertRaises(ValidationError):
                institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            books.publish(tx, school.admin, sid)
            with self.assertRaises(Conflict):
                books.remove_topic(tx, school.admin, sid, d["tree"][1]["topics"][0]["id"])
            tree = syllabus.get_tree(tx, sid)
            self.assertEqual(tree[0]["topics"][0]["title"], "Tiny living things")
            self.assertEqual(tree[0]["topics"][0]["outcomes"][0]["title"], "Name the main groups of microorganisms.")
            self.assertIn(sid, [b["id"] for b in syllabus.available_books(tx)])

    def test_curiosity_style_chapter_files(self):
        """[SY-5] Chapter files laid out like NCERT's newer books ('Curiosity'): the title printed over several lines,
        the chapter number printed large with a QR label (0877CH02) and no 'Chapter N' heading, and a front-matter
        file (hecu1ps.pdf: foreword and contents) uploaded with them. The front matter is not a chapter, chapters keep
        their own numbers, and titles are whole; Claude's fuller title replaces a cut-off one."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        def chapter(name: str, no: int, title_lines: list[str], sections: list[tuple[str, str]]) -> Path:
            p = self.tmp / name
            c = canvas.Canvas(str(p), pagesize=A4)
            c.setFont("Helvetica-Bold", 26)
            for i, line in enumerate(title_lines):
                c.drawString(60, 760 - 30 * i, line)
            c.setFont("Helvetica-Bold", 60)
            c.drawString(470, 720, str(no))
            c.setFont("Helvetica", 7)
            c.drawString(470, 680, f"0877CH{no:02d}")
            c.setFont("Helvetica-Bold", 14)
            c.drawString(60, 640, "Probe and ponder")
            y = 610
            for head, body in sections:
                c.setFont("Helvetica-Bold", 13)
                c.drawString(60, y, head)
                c.setFont("Helvetica", 11)
                for k in range(0, len(body), 90):
                    y -= 16
                    c.drawString(60, y, body[k:k + 90])
                y -= 30
                if y < 120:
                    c.showPage()
                    y = 760
            c.showPage()
            c.save()
            return p

        body = ("Tiny organisms live all around us in soil, water and air. Most of them are too small to see with the "
                "naked eye, so we use a microscope to observe them. Some help us make curd and bread; some cause diseases. ") * 2
        front = self.tmp / "hecu1ps.pdf"
        c = canvas.Canvas(str(front), pagesize=A4)
        for page in (["Foreword", "This textbook is a step towards curiosity."], ["Contents", "1 Exploring the Investigative World of Science", "2 The Invisible Living World: Beyond Our Naked Eye"]):
            for i, line in enumerate(page):
                c.drawString(60, 760 - 20 * i, line)
            c.showPage()
        c.save()
        files = [front,
                 chapter("hecu102.pdf", 2, ["The Invisible Living", "World: Beyond Our", "Naked Eye"],
                         [("What Are Microorganisms?", body), ("Where Do Microorganisms Live?", body), ("How Do Microorganisms Help Us?", body)]),
                 chapter("hecu101.pdf", 1, ["Exploring the Investigative", "World of Science"],
                         [("What Is Science?", body), ("How Do Scientists Investigate?", body), ("Why Do We Measure?", body)])]
        self.assertEqual(outline.big_number(ingest.extract_pages(files[1])), 2)
        self.assertTrue(outline.is_front_matter("hecu1ps.pdf", ingest.extract_pages(front)))
        self.assertFalse(outline.is_front_matter("hecu102.pdf", ingest.extract_pages(files[1])))
        school = self.make_school()
        sid = self.upload(school, files, title="Curiosity")
        d = self.read(school, sid)
        self.assertEqual([(c["code"], c["title"]) for c in d["tree"]],
                         [("1", "Exploring the Investigative World of Science"),
                          ("2", "The Invisible Living World: Beyond Our Naked Eye")])

        # a title the reader can't join on its own (second line not in title case): Claude's fuller title is used
        odd = chapter("hecu103.pdf", 3, ["Health: The Ultimate", "treasure"],
                      [("What Is Health?", body), ("How Do We Stay Healthy?", body)])

        class FullTitles(FakeClaude):
            def outline_chapter(self, course, chapter_title, lines):
                full = {"Health: The Ultimate": "Health: The Ultimate Treasure",
                        "Health: The Ultimate treasure": "Health: The Ultimate Treasure",
                        "The Invisible Living World: Beyond Our Naked Eye": "The Invisible Living World: Beyond Our Naked Eye"}
                out = super().outline_chapter(course, chapter_title, lines)
                for d in out:
                    d["chapter_title"] = full.get(chapter_title, "Something Else Entirely")
                return out
        sid2 = self.upload(school, [front, files[1], odd], title="Curiosity 2", subject="General Science")
        d2 = self.read(school, sid2, FullTitles())
        self.assertEqual([(c["code"], c["title"]) for c in d2["tree"]],
                         [("2", "The Invisible Living World: Beyond Our Naked Eye"), ("3", "Health: The Ultimate Treasure")],
                         "Claude completes a cut-off title, and never replaces a whole one with something else")

    def test_real_curiosity_book_reads_as_printed(self):
        """[SY-5] NCERT's 'Curiosity' Grade 8 Science (13 chapter files plus front matter) reads as printed: every
        chapter's title from the contents page (not the bulletin-board text above chapter 3's title), the book's own
        numbered sections as topics with their wrapped headings whole and no number repeated in the title, sub-sections
        (3.2.1, 3.2.2) inside their section, and sentences such as 'Index (AQI) helps...' no longer cut a chapter short.
        Claude's outline can't override the book's numbered sections. Runs when the PDFs are in data/source/curiosity."""
        folder = PDF.parent / "curiosity"
        if not (folder / "hecu103.pdf").exists():
            self.skipTest("Curiosity PDFs not present")
        files = sorted(folder.glob("hecu1*.pdf"))
        titles = {1: "Exploring the Investigative World of Science", 2: "The Invisible Living World: Beyond Our Naked Eye",
                  3: "Health: The Ultimate Treasure", 4: "Electricity: Magnetic and Heating Effects", 5: "Exploring Forces",
                  6: "Pressure, Winds, Storms, and Cyclones", 7: "Particulate Nature of Matter",
                  8: "Nature of Matter: Elements, Compounds, and Mixtures",
                  9: "The Amazing World of Solutes, Solvents, and Solutions", 10: "Light: Mirrors and Lenses",
                  11: "Keeping Time with the Skies", 12: "How Nature Works in Harmony",
                  13: "Our Home: Earth, a Unique Life Sustaining Planet"}
        sections = {2: 5, 3: 5, 4: 3, 5: 6, 6: 6, 7: 4, 8: 5, 9: 5, 10: 4, 11: 4, 12: 9, 13: 6}
        school = self.make_school()

        class NumberingClaude(FakeClaude):  # outlines as Claude did: numbers in titles, a sub-section as a topic
            def outline_chapter(self, course, chapter_title, lines):
                heads = [i for i, t in enumerate(lines) if re.match(r"^\d+\.\d+(\.\d+)? [A-Z]", t)][:6] or [0]
                return [{"title": lines[i], "start": i, "outcome": f"Outcome for {lines[i]}.", "terms": [],
                         "chapter_title": chapter_title} for i in heads]

        for engine, label in ((OfflineEngine(), "offline"), (NumberingClaude(), "claude")):
            sid = self.upload(school, files, title=f"Curiosity {label}", subject=f"Science {label}")
            d = self.read(school, sid, engine)
            self.assertEqual({int(c["code"]): c["title"] for c in d["tree"]}, titles, label)
            for c in d["tree"]:
                no = int(c["code"])
                codes = [t["code"] for t in c["topics"]]
                if no in sections:
                    self.assertEqual(len(c["topics"]), sections[no], f"{label} ch {no}: {[t['title'] for t in c['topics']]}")
                    self.assertEqual(codes, [f"{no}.{k}" for k in range(1, sections[no] + 1)])
                for t in c["topics"]:
                    self.assertFalse(re.match(r"^\d", t["title"]), f"{label}: number left in {t['title']!r}")
            ch3 = {t["code"]: t["title"] for t in next(c for c in d["tree"] if c["code"] == "3")["topics"]}
            self.assertEqual(ch3, {"3.1": "Health: Is It More Than Not Falling Sick?", "3.2": "How Can We Stay Healthy?",
                                   "3.3": "How Do We Know That We Are Unwell?", "3.4": "Diseases: What Are the Causes and Types?",
                                   "3.5": "How to Prevent and Control Diseases?"}, label)
            wrapped = {t["title"] for c in d["tree"] for t in c["topics"]}
            for whole in ("What Are the Levels of Organisation in the Body of a Living Organism?",
                          "How Does the Moon’s Appearance Change and Why?", "What Makes the Earth Suitable for Life to Exist?"):
                self.assertIn(whole, wrapped, label)

        no_front = outline.file_chapters([(p.name, ingest.extract_pages(p)) for p in files if "ps" not in p.stem])
        got = {c.no: c.title for c in no_front}
        for no in (2, 3, 8, 9, 13):  # without the contents page: the title beside the big chapter number
            self.assertEqual(got[no], titles[no], f"no front matter, ch {no}")

    def test_numbered_sections_split_a_whole_book(self):
        """[SY-5] A whole book in one PDF with no 'Chapter N' headings (Cambridge style: units numbered '1', sections
        '1.1 Photosynthesis') is split into chapters at each unit's first section, titled from the unit's running header.
        The contents page doesn't start a chapter; text recognition's arrow marks ('>» 1.1') and the 'In this topic you
        will' box glued onto a heading are left off topic titles; a heading cut short on its opening page takes the
        version printed most often; and numbered questions at a unit's end ('2.3 The diagram shows …') are not topics."""
        def page(*lines):
            return "\n".join(lines)

        pages = [page("Contents", "1.1 Photosynthesis", "1.2 The carbon cycle", "2.1 Atomic structure and the Periodic Table"),
                 page("Photosynthesis and", "the carbon cycle", ">» 1.1 Photosynthesis Finthitopieyouw", "Plants make food."),
                 page("1 Photosynthesis and the carbon cycle", "1.1 Photosynthesis", "Light is needed for photosynthesis."),
                 page("1 Photosynthesis and the carbon cycle >", "> 1.2 The carbon cycle inthis topic youwils", "Carbon moves."),
                 page("1 Photosynthesis and the carbon cycle", "1.2 The carbon cycle", "Respiration gives out carbon dioxide."),
                 page("1 Photosynthesis and the carbon cycle", "Check your progress", "1.1 Choose the correct word.",
                      "1.3 The diagram shows a leaf."),
                 page("Properties of", "materials", ">» 2.1 Atomic structure and", "Atoms have a nucleus."),
                 page("2 Properties of materials", "2.1 Atomic structure and the Periodic Table", "Protons are positive."),
                 page("2 Properties of materials", "2.1 Atomic structure and the Periodic Table", "Electrons are negative."),
                 page("2 Properties of materials 2", "> 2.2 Trends in groups", "Group 1 metals react with water."),
                 page("2 Properties of materials", "2.2 Trends in groups", "Reactivity increases down the group.")]
        chapters = outline.split_sections(outline.clean_lines(pages))
        self.assertEqual([(c.no, c.title) for c in chapters], [(1, "Photosynthesis and the carbon cycle"), (2, "Properties of materials")])
        self.assertEqual([[t.title for t in outline.find_topics(c)] for c in chapters],
                         [["Photosynthesis", "The carbon cycle"], ["Atomic structure and the Periodic Table", "Trends in groups"]])
        self.assertEqual(chapters[0].lines[0][0], 2, "the contents page (page 1) is not part of chapter 1")
        self.assertEqual(outline.split_sections(outline.clean_lines([page("1.1 Only one", "text")])), [], "one chapter: not this layout")

    def test_real_scanned_cambridge_book(self):
        """[SY-5] A real scanned book (Cambridge Lower Secondary Science 9, 351 pages that are pictures, no text layer)
        is read with text recognition and comes out as printed: its 9 units as chapters and its 41 numbered sections
        as topics, titles clean of what recognition glues on. Uses the page text saved by an earlier full read of
        the book (data/source/cambridge/*.pdf.ocr), as NovaQuest itself does on 'Read again', so the run stays quick.
        Runs when the PDF is in data/source/cambridge."""
        src = PDF.parent / "cambridge" / "cambridge-science-9.pdf"
        saved = src.with_name(src.name + ".ocr")
        if not src.exists() or not saved.is_dir() or not ocr.available():
            self.skipTest("Cambridge scanned book (and its saved page text) not present")
        units = {1: ("Photosynthesis and the carbon cycle", ["Photosynthesis", "More about photosynthesis", "The carbon cycle", "Climate change"]),
                 2: ("Properties of materials", ["Atomic structure and the Periodic Table", "Trends in groups within the Periodic Table",
                                                 "Why elements react to form compounds", "Simple and giant structures"]),
                 3: ("Forces and energy", ["Density", "Heat and temperature", "Conservation of energy", "Moving from hot to cold",
                                           "Ways of transferring thermal energy", "Cooling by evaporation"]),
                 4: ("Maintaining life", ["Plants and water", "Transpiration", "Excretion in humans", "Keeping a fetus healthy"]),
                 5: ("Reactivity", ["Reactivity and displacement reactions", "Using the reactivity series and displacement reactions",
                                    "Salts", "Other ways of making salts", "Rearranging atoms"]),
                 6: ("Sound and space", ["Loudness and pitch of sound", "Interference of sound", "Formation of the Moon", "Nebulae", "Tectonics"]),
                 7: ("Genes and inheritance", ["Chromosomes, genes and DNA", "Gametes and inheritance", "Variation", "Natural selection"]),
                 8: ("Rates of reaction", ["Measuring the rate of reaction", "Surface area and the rate of reaction",
                                          "Temperature and the rate of reaction", "Concentration and the rate of reaction"]),
                 9: ("Electricity", ["Parallel circuits", "Current and voltage in parallel circuits", "Resistance", "Practical circuits"])}
        school = self.make_school()
        sid = self.upload(school, [src], board="Cambridge", grade="9", title="Cambridge Science 9")
        with self.db.tenant(school.inst) as tx:
            stored = tx.val("SELECT stored_as FROM book_files WHERE syllabus_id=%s", [sid])
        shutil.copytree(saved, self.storage / str(school.inst) / str(sid) / (stored + ".ocr"))
        d = self.read(school, sid)
        self.assertEqual(d["book"]["status"], "review", d["book"]["meta"].get("error"))
        self.assertEqual(d["book"]["meta"]["scanned"], 351)
        self.maxDiff = None
        got = {int(c["code"]): (c["title"].lower(), [t["title"].lower() for t in c["topics"]]) for c in d["tree"]}
        self.assertEqual(got, {n: (t.lower(), [x.lower() for x in ts]) for n, (t, ts) in units.items()})
        with self.db.tenant(school.inst) as tx:
            n = tx.val("SELECT count(*) FROM source_passages WHERE syllabus_id=%s", [sid])
        self.assertGreater(n, 200)

    def test_claude_topics_lose_printed_numbers_and_sub_sections(self):
        """[SY-5] When Claude outlines a chapter that doesn't number its own sections, a printed number is taken off
        each topic title and a sub-section ('3.2.2 …') is not made a topic."""
        ch = outline.Chapter(3, "Health", [(1, f"line {i}") for i in range(30)], "x.pdf")
        drafted = [{"title": "3.1 Health: Is It More Than Not Falling Sick?", "start": 0},
                   {"title": "3.2 How Can We Stay Healthy?", "start": 8},
                   {"title": "3.2.2 Keep the environment clean", "start": 12},
                   {"title": "3.3 How Do We Know That We Are Unwell?", "start": 20}]
        topics = books._topics_from_draft(ch, drafted)
        self.assertEqual([t.title for t in topics], ["Health: Is It More Than Not Falling Sick?", "How Can We Stay Healthy?",
                                                      "How Do We Know That We Are Unwell?"])

    def test_class_studies_several_books(self):
        """[SY-6] A class can study several books (for example NCERT Science and the school's own book): the student's
        Learn tab has one tab per subject, Ask NOVA searches the class's books and cites the right one, practice works
        on any book, and a teacher sets work from one book of their class at a time."""
        if not PDF.exists():
            self.skipTest("textbook PDF not present")
        school = self.make_school()
        sid = self.upload(school, chapter_pdfs(self.tmp, (2,)), subject="Biology", board="ICSE", title="Life Science")
        self.read(school, sid)
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [school.syllabus_id, sid])
            st = school.student(0)
            self.assertEqual([b["subject"] for b in learn.student_books(tx, st)], ["Science", "Biology"])
            own = learn.topics_for_student(tx, st, sid)
            self.assertTrue(own and all(c["book_id"] == sid for c in own))
            every = learn.topics_for_student(tx, st, all_books=True)
            self.assertEqual({c["book_id"] for c in every}, {school.syllabus_id, sid})
            path.annotate(tx, st.user_id, every)
            topic = own[0]["topics"][0]
            p = path.topic_path(tx, st.user_id, syllabus.get_topic(tx, topic["id"]))
            self.assertEqual([s["key"] for s in p["steps"]], ["practice", "understand"],
                             "no picture lesson or NOVA mix-up until the admin approves them")
            mission = learn.practice(tx, st, topic["id"])
            self.assertGreaterEqual(len(mission["questions"]), 3)
        r = learn.ask(self.db, self.engine, st, "what is fermentation")
        self.assertFalse(r.declined)
        self.assertTrue(any(c.startswith(("Life Science, Ch 2", "NCERT")) for c in r.citations), r.citations)
        r = learn.ask(self.db, self.engine, st, "explain pasteurisation", topic["id"])
        self.assertTrue(r.citations and r.citations[0].startswith("Life Science, Ch 2"), r.citations)
        with self.db.tenant(school.inst) as tx:
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id, syllabus_id=sid)
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[topic["id"]], kind="quiz", difficulty="easy", count=3, title="")
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT syllabus_id FROM assignments WHERE id=%s", [aid]), sid)
        with self.assertRaises(ValidationError):
            questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                          topic_ids=[topic["id"], school.topics["8.5"]], kind="quiz", difficulty="easy",
                                          count=3, title="")

    def test_one_pdf_book_scanned_and_wrong_files(self):
        """[SY-5] A whole book in one PDF is split at its 'Chapter N' headings and its ALL-CAPS section headings become
        topics. A non-PDF is refused at upload; a PDF with no words at all (not even on its pictures) is marked failed
        with a plain reason; a book that was never published can be deleted with its files."""
        school = self.make_school()
        sid = self.upload(school, [made_up_book(self.tmp / "physics.pdf")], subject="Physics", board="IGCSE", grade="9",
                          title="Physics for You")
        d = self.read(school, sid)
        self.assertEqual([(c["code"], c["title"]) for c in d["tree"]], [("1", "Motion and Measurement"), ("2", "Simple Machines")])
        self.assertEqual([t["title"] for t in d["tree"][1]["topics"]], ["Levers", "Pulleys", "Inclined Planes"])
        with self.assertRaises(ValidationError):
            self.upload(school, [self._file("notes.pdf", b"hello, not a pdf")])
        scan = self.tmp / "scan.pdf"
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(scan))
        c.rect(50, 50, 400, 600)
        c.showPage()
        c.save()
        bad = self.upload(school, [scan], title="A scanned book")
        d = self.read(school, bad)
        self.assertEqual(d["book"]["status"], "failed")
        self.assertIn("even with text recognition", d["book"]["meta"]["error"])
        folder = self.storage / str(school.inst) / str(bad)
        self.assertTrue(folder.exists())
        books.delete(self.db, school.admin, bad, self.storage)
        self.assertFalse(folder.exists())
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(NotFound):
                syllabus.get_syllabus(tx, bad)

    def test_scanned_book_is_read_with_text_recognition(self):
        """[SY-5] A book whose pages are pictures (a scan) is read with OCR: the book page shows 'page n of N' while
        it reads, then the chapters and topics are found as for a text PDF, and the review note says the pages were
        scanned. Each page's text is saved, so reading the book again doesn't read the pages again. A text PDF is
        never sent to OCR. Without Tesseract the book fails with what to install, and 'Read again' reads it once
        Tesseract is there."""
        from unittest import mock

        self.assertTrue(ocr.available(), "Tesseract and pdftoppm are installed on the test machine")
        school = self.make_school()
        scan = scanned_copy(made_up_book(self.tmp / "physics.pdf"), self.tmp / "physics-scan.pdf")
        self.assertLess(sum(len(p.split()) for p in ingest.extract_pages(scan)), 5, "no text layer")
        self.assertEqual(ocr.scanned_pages(ingest.extract_pages(self.tmp / "physics.pdf")), [], "text PDFs skip OCR")
        self.assertEqual(ocr.scanned_pages(["word " * 50] * 8 + [""] * 2), [], "a few blank pages are not a scan")

        sid = self.upload(school, [scan], subject="Physics", board="IGCSE", grade="9", title="Physics scan")
        seen = []
        real = books._progress

        def watch(db, inst, sid_, **meta):
            seen.append(dict(meta))
            real(db, inst, sid_, **meta)

        with mock.patch.object(books, "_progress", watch):
            d = self.read(school, sid)
        self.assertEqual(d["book"]["status"], "review", d["book"]["meta"].get("error"))
        self.assertEqual([(c["code"], c["title"]) for c in d["tree"]], [("1", "Motion and Measurement"), ("2", "Simple Machines")])
        self.assertEqual([t["title"] for t in d["tree"][1]["topics"]], ["Levers", "Pulleys", "Inclined Planes"])
        pages = d["files"][0]["pages"]
        self.assertEqual(d["book"]["meta"]["scanned"], pages)
        self.assertIn({"stage": "scanning", "done": 0, "total": pages}, seen)
        self.assertIn({"done": pages}, seen, "progress reaches the last page")
        with self.db.tenant(school.inst) as tx:
            n = tx.val("SELECT count(*) FROM source_passages WHERE syllabus_id=%s", [sid])
        self.assertGreater(n, 0)
        stored = self.storage / str(school.inst) / str(sid)
        self.assertEqual(len(list(stored.glob("*.ocr/*.txt"))), pages, "each page's text is saved")

        # without Tesseract: a plain reason, then Read again once it is installed (saved pages are not read again)
        sid2 = self.upload(school, [scan], subject="Physics", board="IGCSE", grade="10", title="Physics scan 2")
        with mock.patch.object(ocr, "tesseract", lambda: None):
            d = self.read(school, sid2)
        self.assertEqual(d["book"]["status"], "failed")
        self.assertEqual(d["book"]["meta"]["error"], books.SCAN_HELP.format(why="Tesseract OCR isn't installed"))
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):
                books.read_again(tx, school.admin, sid)  # only a failed book
            books.read_again(tx, school.admin, sid2)
            self.assertEqual(books.own_book(tx, sid2)["status"], "processing")
            self.assertNotIn("error", books.own_book(tx, sid2)["meta"])
        d = self.read(school, sid2)
        self.assertEqual(d["book"]["status"], "review")
        with self.db.tenant(school.inst) as tx:  # read once more from the saved text: no page is read again
            tx.exec("UPDATE syllabi SET status='failed' WHERE id=%s", [sid2])
            tx.exec("DELETE FROM source_passages WHERE syllabus_id=%s", [sid2])
            tx.exec("UPDATE syllabus_nodes SET parent_id=NULL WHERE syllabus_id=%s", [sid2])
            tx.exec("DELETE FROM syllabus_nodes WHERE syllabus_id=%s", [sid2])
            books.read_again(tx, school.admin, sid2)
        with mock.patch.object(ocr, "_read_page", side_effect=AssertionError("read again")):
            d = self.read(school, sid2)
        self.assertEqual(d["book"]["status"], "review")
        self.assertEqual(len(d["tree"]), 2)

    def test_tesseract_without_english_uses_novaquests_own_copy(self):
        """[SY-5] Windows' Tesseract installer, run silently, installs no language, so every page failed ('returned
        non-zero exit status 1') and the book said 'upload a clearer copy'. Now: a Tesseract with no English is
        reported as such (book and `nq status`); if pages still all fail, the book shows Tesseract's own reason, not
        'a clearer copy'; and NovaQuest's own English file (NQ_TESSDATA, put there by the installer) is used, so the
        book reads."""
        import os
        from unittest import mock

        empty = self.tmp / "no-languages"
        empty.mkdir()
        mine = self.tmp / "nq-tessdata"
        mine.mkdir()
        system = next(Path("/usr/share/tesseract-ocr").glob("*/tessdata/eng.traineddata"))
        shutil.copy(system, mine / "eng.traineddata")
        school = self.make_school()
        scan = scanned_copy(made_up_book(self.tmp / "physics.pdf"), self.tmp / "physics-scan.pdf")
        ocr._has_english.cache_clear()
        try:
            with mock.patch.dict(os.environ, {"TESSDATA_PREFIX": str(empty), "NQ_TESSDATA": "", "LOCALAPPDATA": ""}):
                self.assertEqual(ocr.problem(), "Tesseract has no English language file")
                sid = self.upload(school, [scan], subject="Physics", board="IGCSE", grade="9", title="No English")
                d = self.read(school, sid)
                self.assertEqual(d["book"]["status"], "failed")
                self.assertIn("Tesseract has no English language file", d["book"]["meta"]["error"])
                with mock.patch.object(ocr, "_has_english", lambda exe, data: True):  # a check that passes wrongly
                    sid2 = self.upload(school, [scan], subject="Physics", board="IGCSE", grade="10", title="All fail")
                    d = self.read(school, sid2)
                err = d["book"]["meta"]["error"]
                self.assertIn("couldn't read any page", err)
                self.assertIn("Failed loading language", err, "Tesseract's own reason")
                self.assertNotIn("clearer copy", err)
                self.assertFalse(list((self.storage / str(school.inst) / str(sid2)).glob("*.ocr/*.txt")), "failures aren't saved")
            ocr._has_english.cache_clear()
            with mock.patch.dict(os.environ, {"TESSDATA_PREFIX": str(empty), "NQ_TESSDATA": str(mine)}):
                self.assertEqual(ocr.tessdata(), str(mine))
                self.assertTrue(ocr.available())
                with self.db.tenant(school.inst) as tx:
                    books.read_again(tx, school.admin, sid)
                d = self.read(school, sid)
            self.assertEqual(d["book"]["status"], "review", d["book"]["meta"].get("error"))
            self.assertEqual(len(d["tree"]), 2)
        finally:
            ocr._has_english.cache_clear()

    def _file(self, name: str, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_books_are_private_to_their_school(self):
        """[SY-5][NF-ISO] A school's book, its topics, passages and lessons are invisible to every other school, even
        by id: database row-level security, not just the screens. The shared NCERT book stays visible to all."""
        a = self.make_school("SUN")
        b = self.make_school("MOON")
        sid = self.upload(a, [made_up_book(self.tmp / "p.pdf")], title="Private Book")
        self.read(a, sid)
        with self.db.tenant(a.inst) as tx:
            books.publish(tx, a.admin, sid)
            topic = syllabus.get_tree(tx, sid)[0]["topics"][0]["id"]
        with self.db.tenant(b.inst) as tx:
            for table, col in (("syllabi", "id"), ("syllabus_nodes", "syllabus_id"), ("source_passages", "syllabus_id"),
                               ("book_files", "syllabus_id")):
                self.assertEqual(tx.val(f"SELECT count(*) FROM {table} WHERE {col}=%s", [sid]), 0, table)
            for fn in (lambda: syllabus.get_syllabus(tx, sid), lambda: syllabus.get_tree(tx, sid),
                       lambda: syllabus.get_topic(tx, topic), lambda: books.detail(tx, sid)):
                with self.assertRaises(NotFound):
                    fn()
            self.assertFalse(search.search(tx, sid, "levers and pulleys").hits)
            with self.assertRaises(ValidationError):
                institutions.set_class_subjects(tx, b.admin, b.class_id, [sid])
            self.assertTrue(syllabus.get_tree(tx, b.syllabus_id), "the shared NCERT book is still there")
            with self.assertRaises(Exception):  # writing into another school's book is refused by the database
                tx.exec("""INSERT INTO syllabus_nodes(syllabus_id, institution_id, kind, code, title, ord, path)
                           VALUES (%s,%s,'chapter','99','x',99,'c99')""", [sid, a.inst])

    def test_claude_drafts_lessons_that_wait_for_approval(self):
        """[SY-5][LN-10] With Claude, NovaQuest drafts each topic's chapter outline, picture lesson, quick checks and
        NOVA's mix-ups. Every draft is validated (unsafe or undrawable parts are dropped) and nothing reaches students
        until the admin approves it; then the topic gains its Learn-in-pictures and Teach-NOVA steps. Drafting runs in
        small batches that queue the next one, and stops cleanly without Claude."""
        school = self.make_school()
        sid = self.upload(school, [made_up_book(self.tmp / "p.pdf")], subject="Physics", title="Physics for You")
        d = self.read(school, sid, FakeClaude())
        self.assertEqual(d["book"]["meta"]["method"], "claude")
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            books.request_lessons(tx, school.admin, sid)
            topics = [t for c in syllabus.get_tree(tx, sid) for t in c["topics"]]
        books.draft_lessons(self.db, OfflineEngine(), school.inst, sid)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(books.detail(tx, sid)["book"]["meta"]["lessons"]["stage"], "needs_claude")
        fake = FakeClaude(bad_codes=(topics[0]["code"],))
        books.draft_lessons(self.db, fake, school.inst, sid, batch=4)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(len(fake.drafted), 4)
            self.assertTrue(tx.val("SELECT 1 FROM jobs WHERE kind='book_lessons' AND status='queued' AND dedupe_key LIKE %s",
                                   [f"book_lessons:{sid}:%"]), "the rest continues in the next job")
        with self.db.tenant(school.inst) as tx:  # the next batch, as the worker runs it (with the run's tried topics)
            nxt = tx.val("SELECT payload FROM jobs WHERE kind='book_lessons' AND status='queued' ORDER BY id DESC LIMIT 1")
        books.draft_lessons(self.db, fake, school.inst, sid, batch=4, skip=nxt.get("skip"), failed_ids=nxt.get("failed"))
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            d = books.detail(tx, sid)
            self.assertEqual(d["book"]["meta"]["lessons"]["stage"], "ready")
            self.assertEqual(len(fake.drafted), len(topics))
            first, second = topics[0], topics[1]
            dr = books.topic_drafts(tx, sid, first["id"])
            self.assertIsNone(dr["pictures"], "a lesson with an undrawable picture is dropped")
            self.assertEqual(len(dr["mixups"]), 1, "an incomplete mix-up is dropped")
            self.assertEqual(pictures.lessons_for(tx, [{**second, "syllabus_id": sid}]), {})
            self.assertEqual(teach.available_misconceptions(tx, second["id"]), [])
            topic2 = syllabus.get_topic(tx, second["id"])
            self.assertNotIn("pictures", [s["key"] for s in path.topic_path(tx, st.user_id, topic2)["steps"]])
            self.assertGreater(books.approve(tx, school.admin, sid, second["id"]), 0)
            self.assertIn(second["id"], pictures.lessons_for(tx, [{**second, "syllabus_id": sid}]))
            self.assertEqual([s["key"] for s in path.topic_path(tx, st.user_id, topic2)["steps"]],
                             ["pictures", "practice", "understand", "teach"])
            self.assertEqual(teach.available_misconceptions(tx, second["id"])[0]["id"], f"B{sid}-{second['code']}-1")
            deck = pictures.deck(tx, st, second["id"])
            self.assertNotIn("answer", deck["lesson"]["quiz"][0], "quiz answers stay on the server")
            lesson = learn.lesson(tx, st, second["id"])
            self.assertEqual(lesson["explain"], f"{second['title']} explained simply.")
            books.approve(tx, school.admin, sid)
            self.assertEqual(books.detail(tx, sid)["drafts"], 0)

    def test_drafting_runs_a_few_topics_at_once_and_counts_failures_honestly(self):
        """[SY-5][NF-PERF] Lesson drafting runs a few topics at once (a Claude draft takes about a minute). A topic
        Claude couldn't draft is shown as 'couldn't be drafted', not counted as done, and isn't retried in the same
        run; the run then pauses so the admin can continue, which tries it again."""
        import time as _time

        from novaquest.worker import _book_lessons
        school = self.make_school()
        sid = self.upload(school, [made_up_book(self.tmp / "p.pdf")], subject="Physics", title="Physics for You")
        self.read(school, sid, FakeClaude())
        with self.db.tenant(school.inst) as tx:
            topics = [t for c in syllabus.get_tree(tx, sid) for t in c["topics"]]
        self.assertGreaterEqual(len(topics), 4)

        class Slow(FakeClaude):
            def __init__(self, fail=()):
                super().__init__()
                self.fail, self.live, self.peak = set(fail), 0, 0

            def draft_topic(self, topic):
                self.live += 1
                self.peak = max(self.peak, self.live)
                _time.sleep(0.4)
                self.live -= 1
                return None if topic.code in self.fail else super().draft_topic(topic)
        slow = Slow(fail={topics[1]["code"]})
        t0 = _time.perf_counter()
        books.draft_lessons(self.db, slow, school.inst, sid, batch=len(topics))
        took = _time.perf_counter() - t0
        self.assertGreaterEqual(slow.peak, 2, "topics are drafted at the same time")
        self.assertLess(took, 0.4 * len(topics) * 0.8)
        with self.db.tenant(school.inst) as tx:
            L = books.detail(tx, sid)["book"]["meta"]["lessons"]
        self.assertEqual((L["done"], L["failed"], L["stage"]), (len(topics) - 1, 1, "paused"))
        self.assertEqual(slow.drafted.count(topics[1]["code"]), 0, "the failed draft was not saved")
        ok = Slow()
        with self.db.tenant(school.inst) as tx:
            books.request_lessons(tx, school.admin, sid)
            job = tx.one("SELECT id, institution_id, payload FROM jobs WHERE kind='book_lessons' ORDER BY id DESC LIMIT 1")
        books.draft_lessons(self.db, ok, school.inst, sid, skip=job["payload"].get("skip"))
        self.assertEqual(ok.drafted, [topics[1]["code"]], "continuing tries only the topic that failed")
        with self.db.tenant(school.inst) as tx:
            L = books.detail(tx, sid)["book"]["meta"]["lessons"]
        self.assertEqual((L["done"], L["failed"], L["stage"]), (len(topics), 0, "ready"))
        self.assertTrue(callable(_book_lessons))
        # a restart mid-run: the job is left 'running'; the page offers Continue, and the worker requeues it on start
        from novaquest.services import jobs
        with self.db.tenant(school.inst) as tx:
            books.request_lessons(tx, school.admin, sid)
            self.assertTrue(books.detail(tx, sid)["drafting_now"])
        self.assertTrue(jobs.claim(self.db))
        with self.db.tenant(school.inst) as tx:
            self.assertTrue(books.detail(tx, sid)["drafting_now"], "running counts as drafting")
            tx.exec("UPDATE jobs SET status='done' WHERE kind='book_lessons'")
            tx.exec("UPDATE syllabi SET meta = meta || '{\"lessons\": {\"stage\": \"drafting\", \"done\": 1, \"total\": 9}}' WHERE id=%s", [sid])
            self.assertFalse(books.detail(tx, sid)["drafting_now"], "no job left: the page offers Continue drafting")
        self.clock.advance(minutes=1)
        with self.db.tenant(school.inst) as tx:
            books.request_lessons(tx, school.admin, sid)
        jobs.claim(self.db)
        self.assertGreaterEqual(jobs.requeue_interrupted(self.db), 1)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT status FROM jobs WHERE kind='book_lessons' ORDER BY id DESC LIMIT 1"), "queued")

    def test_topic_without_pictures_is_not_counted_as_waiting_and_can_be_redrafted(self):
        """[SY-5] A topic whose picture lesson couldn't be drawn isn't counted as an item to approve on the Books list
        (it said '1 to approve' with nothing to approve). 'Draft any missing topics' tries its pictures again without
        un-approving its quick checks or mix-ups, and each run tries it only once. The book page labels quick checks
        that wait for approval."""
        from tests.live import Client, LiveServer
        school = self.make_school()
        sid = self.upload(school, [made_up_book(self.tmp / "p.pdf")], subject="Physics", title="Physics for You")
        self.read(school, sid, FakeClaude())
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            topics = [t for c in syllabus.get_tree(tx, sid) for t in c["topics"]]
        bad = topics[0]
        fake = FakeClaude(bad_codes=(bad["code"],))
        books.draft_lessons(self.db, fake, school.inst, sid, batch=len(topics))
        with self.db.tenant(school.inst) as tx:
            d = books.detail(tx, sid)
            self.assertEqual(d["pics"][bad["id"]], "failed")
            self.assertEqual(d["book"]["meta"]["lessons"]["no_pictures"], 1)
            self.assertEqual(d["book"]["meta"]["lessons"]["stage"], "ready")
            self.assertEqual(books.approve(tx, school.admin, sid), d["drafts"])
            listed = next(b for b in books.list_books(tx) if b["id"] == sid)
            self.assertEqual((listed["drafts"], books.detail(tx, sid)["drafts"]), (0, 0), "nothing left to approve, on both pages")
            self.assertEqual(tx.val("SELECT status FROM lessons WHERE node_id=%s", [bad["id"]]), "approved")
            books.request_lessons(tx, school.admin, sid)
        good = FakeClaude()
        books.draft_lessons(self.db, good, school.inst, sid, batch=len(topics))
        self.assertEqual(good.drafted, [bad["code"]], "only the topic without pictures is tried again")
        with self.db.tenant(school.inst) as tx:
            d = books.detail(tx, sid)
            self.assertEqual(d["pics"][bad["id"]], "draft", "it now has a picture lesson to check")
            self.assertEqual(tx.val("SELECT status FROM lessons WHERE node_id=%s", [bad["id"]]), "approved",
                             "its approved quick checks stay approved")
            self.assertEqual(next(b for b in books.list_books(tx) if b["id"] == sid)["drafts"], d["drafts"])
            self.assertEqual(d["book"]["meta"]["lessons"]["no_pictures"], 0)
            tx.exec("UPDATE lessons SET status='draft' WHERE node_id=%s", [topics[1]["id"]])
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("Quick checks to check", html)
            self.assertIn("Quick checks ✓", html)
        finally:
            live.stop()

    def test_preview_back_and_next_sit_under_the_card(self):
        """[SY-5][LN-10][UX-1] On the staff previews of a picture lesson (teacher preview, and the admin's check of a
        drafted topic) the Back/Next bar sits under the card instead of floating over it (it was placed to clear the
        students' phone menu bar, which staff pages don't have). Students keep the bar fixed above their menu bar."""
        import asyncio

        from playwright.async_api import async_playwright

        from tests.live import Client, LiveServer
        from tests.test_browser import LAPTOP, PHONE
        school = self.make_school()
        sid = self.upload(school, [made_up_book(self.tmp / "p.pdf")], subject="Physics", title="Physics for You")
        self.read(school, sid, FakeClaude())
        with self.db.tenant(school.inst) as tx:
            first = [t for c in syllabus.get_tree(tx, sid) for t in c["topics"]][0]
        books.draft_lessons(self.db, FakeClaude(), school.inst, sid, batch=1)
        live = LiveServer(self.settings, self.db)
        tid = school.topics["8.5"]

        async def go():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                out = {}
                page = await (await b.new_context(**LAPTOP)).new_page()
                await page.goto(live.url + "/login")
                await page.fill("#email", "rao@sun.test")
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_load_state()
                await page.goto(f"{live.url}/teacher/syllabus/topics/{tid}/pictures")
                await page.evaluate("() => sessionStorage.clear()")
                await page.goto(f"{live.url}/teacher/syllabus/topics/{tid}/pictures")
                while await page.is_visible("[data-next]"):
                    await page.click("[data-next]")
                out["teacher"] = await page.evaluate("""() => {
                    const nav = document.querySelector('[data-nav]');
                    const card = [...document.querySelectorAll('[data-card]')].find(c => !c.hidden);
                    return {pos: getComputedStyle(nav).position, gap: nav.getBoundingClientRect().top - card.getBoundingClientRect().bottom}; }""")
                ph = await (await b.new_context(**PHONE)).new_page()
                await ph.goto(live.url + "/login/student")
                await ph.fill("#school", "SUN")
                await ph.fill("#klass", "8B")
                await ph.fill("#roll", "1")
                await ph.fill("#pin", school.students[0]["pin"])
                await ph.click("button[type=submit]")
                await ph.wait_for_url("**/student")
                await ph.goto(f"{live.url}/student/learn/{tid}/pictures")
                out["student"] = await ph.evaluate("getComputedStyle(document.querySelector('[data-nav]')).position")
                await b.close()
                return out
        try:
            out = asyncio.run(go())
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get(f"/admin/books/{sid}/topics/{first['id']}")[1]
        finally:
            live.stop()
        self.assertEqual(out["teacher"]["pos"], "static")
        self.assertGreaterEqual(out["teacher"]["gap"], 0, "the bar is under the last card, not over it")
        self.assertEqual(out["student"], "sticky", "students keep Back/Next within reach")
        self.assertIn('class="deck preview"', html, "the admin's check uses the same preview layout")

    def test_claude_prompts_for_outline_and_drafts(self):
        """[SY-5][NF-MODEL] The Claude engine asks for a chapter's topics by line number and for a topic's material in
        the drawable picture kinds only; it names the course, and falls back cleanly when Claude fails."""
        from novaquest.ai.claude import ClaudeEngine
        from novaquest.ai.provider import ClaudeProvider
        from tests.test_ai import FakeServer, claude_reply

        def respond(body, headers):
            system = " ".join(b["text"] for b in body["system"]) if isinstance(body["system"], list) else body["system"]
            if "split one textbook chapter" in system:
                self.assertIn("Course: Grade 9 Physics (IGCSE)", system)
                return claude_reply(json.dumps({"chapter_title": "Simple Machines", "topics": [
                    {"title": "Levers", "start_line": 2, "outcome": "Explain how a lever works.", "terms": ["fulcrum"]},
                    {"title": "Ghost", "start_line": 999}]}))
            if "write learning material" in system:
                self.assertIn("ONLY these kinds", system)
                return 500, {"error": "overloaded"}
            return claude_reply("{}")

        server = FakeServer(respond)
        try:
            eng = ClaudeEngine(ClaudeProvider("sk-ant-test", server.url, "fast", "strong", 5.0))
            out = eng.outline_chapter("Grade 9 Physics (IGCSE)", "Simple Machines", ["Chapter 2", "Simple Machines", "LEVERS", "A lever…"])
            self.assertEqual([(t["title"], t["start"]) for t in out], [("Levers", 2)], "out-of-range topics are dropped")
            from novaquest.ai.types import TopicCtx
            ctx = TopicCtx(1, "2.1", "Levers", "Explain levers.", ("fulcrum",), 2, "Simple Machines", None,
                           ({"id": 1, "ref": "p. 1", "body": "A lever turns about a fulcrum."},), course="Grade 9 Physics (IGCSE)")
            self.assertIsNone(eng.draft_topic(ctx), "Claude failing means no draft, never a broken one")
        finally:
            server.close()


class BooksWeb(NQTestCase):
    def setUp(self):
        super().setUp()
        from tests.live import LiveServer
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-booksweb-"))
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(books.storage_dir(self.settings.data_dir), ignore_errors=True)
        super().tearDown()

    def test_admin_uploads_publishes_and_gives_book_to_a_class(self):
        """[SY-5][SY-6] Over HTTP: the school admin's Books page uploads several PDFs in one go, the book page shows
        reading progress then the topics to check, the admin renames a topic and publishes; on School setup the class
        gets the new subject; the teacher picks 'class · subject' to set work; the student's Learn tab gains a tab per
        subject. Other roles can't open the Books pages."""
        from novaquest import worker
        from tests.base import PASSWORD
        from tests.live import Client

        school = self.make_school()
        a = Client(self.live.url)
        a.login("admin@sun.test", PASSWORD)
        status, html, _, _ = a.get("/admin/books")
        self.assertEqual(status, 200)
        self.assertIn('name="files[]"', html)
        self.assertIn('multiple', html)
        pdf = made_up_book(self.tmp / "physics.pdf").read_bytes()
        status, html, _, url = a.post_multipart("/admin/books", {"board": "IGCSE", "subject": "Physics", "grade": "8",
                                                                 "title": "Physics for You"},
                                                [("files[]", ("part-1.pdf", pdf, "application/pdf")),
                                                 ("files[]", ("notes.txt", b"not a pdf", "text/plain"))])
        self.assertEqual(status, 422, "one bad file refuses the whole upload")
        self.assertIn("notes.txt isn&#39;t a PDF", html)
        status, html, _, url = a.post_multipart("/admin/books", {"board": "IGCSE", "subject": "Physics", "grade": "8",
                                                                 "title": "Physics for You"},
                                                [("files[]", ("physics.pdf", pdf, "application/pdf"))])
        self.assertEqual(status, 200)
        sid = int(re.search(r"/admin/books/(\d+)", url).group(1))
        self.assertIn("Reading the book", html)
        self.assertIn('http-equiv="refresh"', html)
        worker.run_due(self.db, self.settings)
        html = a.get(f"/admin/books/{sid}")[1]
        self.assertIn("Check the topics, then publish", html)
        self.assertNotIn('http-equiv="refresh"', html)
        with self.db.tenant(school.inst) as tx:
            node = syllabus.get_tree(tx, sid)[0]["topics"][0]["id"]
        a.post(f"/admin/books/{sid}/rename", {"node_id": node, "title": "How far and which way", "outcome": "Tell distance from displacement."})
        status, html, _, _ = a.post(f"/admin/books/{sid}/publish")
        self.assertIn("Published", html)
        html = a.get("/admin")[1]
        self.assertIn("Physics · IGCSE Grade 8", html)
        with self.db.tenant(school.inst) as tx:
            ncert = school.syllabus_id
        a.post("/admin/subjects", {"class_id": school.class_id, "books[]": [ncert, sid]})
        a.post("/admin/map", {"teacher_id": school.teacher.user_id, "class_id": school.class_id, "syllabus_id": sid})
        html = a.get("/admin")[1]
        self.assertRegex(html, r"Science <span class=\"muted\">\(NCERT\)</span>, Physics <span class=\"muted\">\(IGCSE\)</span>")
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        html = t.get("/teacher/assignments/new")[1]
        self.assertIn(f'value="{school.class_id}:{sid}"', html)
        html = t.get(f"/teacher/assignments/new?for={school.class_id}:{sid}")[1]
        self.assertIn("How far and which way", html)
        self.assertNotIn("Crop Production", html, "only the chosen subject's topics")
        html = t.get(f"/teacher/syllabus?for={school.class_id}:{sid}")[1]
        self.assertIn("Physics · IGCSE Grade 8", html)
        st = Client(self.live.url)
        st.login_student("SUN", "8B", "1", school.students[0]["pin"])
        html = st.get("/student/learn")[1]
        self.assertIn('aria-label="Subjects"', html)
        self.assertIn(f'href="/student/learn?book={sid}"', html)
        html = st.get(f"/student/learn?book={sid}")[1]
        self.assertIn("How far and which way", html)
        self.assertIn("Class 8B · Science, Physics", html)
        books.draft_lessons(self.db, FakeClaude(), school.inst, sid, batch=50)
        html = a.get(f"/admin/books/{sid}")[1]
        self.assertRegex(html, r"Approve all \d+")
        self.assertIn("Pictures to check", html)
        html = a.get(f"/admin/books/{sid}/topics/{node}")[1]
        self.assertIn("Approve this topic", html)
        self.assertIn("Which is right?", html)
        self.assertIn("✓", html, "the admin sees the answers")
        self.assertIn("I think levers make work disappear", html)
        html = a.post(f"/admin/books/{sid}/approve", {"topic_id": node, "stay": "1"})[1]
        self.assertIn("Approved", html)
        html = st.get(f"/student/learn/{node}")[1]
        self.assertIn(f'href="/student/learn/{node}/pictures">Start: Learn in pictures', html)
        html = st.get(f"/student/learn/{node}/pictures")[1]
        self.assertIn("The big idea of How far and which way", html)
        for c in (t, st):
            status, _, _, _ = c.get("/admin/books")
            self.assertIn(status, (403, 200))
            self.assertNotIn("Upload a book", c.get("/admin/books")[1])

    def test_scanned_book_progress_and_read_again_over_http(self):
        """[SY-5][UX-1] Over HTTP: while a scanned book's pages are read the book page says 'page n of N' and updates by
        itself; a book that couldn't be read (no Tesseract) shows the reason and a Read again button, which reads it
        again; the review note says the pages were scanned."""
        from unittest import mock

        from novaquest import worker
        from tests.base import PASSWORD
        from tests.live import Client

        school = self.make_school()
        a = Client(self.live.url)
        a.login("admin@sun.test", PASSWORD)
        scan = scanned_copy(made_up_book(self.tmp / "physics.pdf"), self.tmp / "physics-scan.pdf")
        _, _, _, url = a.post_multipart("/admin/books", {"board": "IGCSE", "subject": "Physics", "grade": "9", "title": "Scan"},
                                        [("files[]", ("scan.pdf", scan.read_bytes(), "application/pdf"))])
        sid = int(re.search(r"/admin/books/(\d+)", url).group(1))
        with self.db.tenant(school.inst) as tx:
            tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s",
                    [json.dumps({"stage": "scanning", "done": 40, "total": 351}), sid])
        html = a.get(f"/admin/books/{sid}")[1]
        self.assertIn("page 40 of 351", html)
        self.assertIn('http-equiv="refresh"', html)
        with mock.patch.object(ocr, "tesseract", lambda: None):
            worker.run_due(self.db, self.settings)
        html = a.get(f"/admin/books/{sid}")[1]
        self.assertIn("Tesseract OCR", html)
        self.assertIn(f'action="/admin/books/{sid}/read"', html)
        html = a.post(f"/admin/books/{sid}/read")[1]
        self.assertIn("Reading the book again", html)
        worker.run_due(self.db, self.settings)
        html = a.get(f"/admin/books/{sid}")[1]
        self.assertIn("Check the topics, then publish", html)
        self.assertIn("scanned pictures, so their words were read by text recognition", html)
