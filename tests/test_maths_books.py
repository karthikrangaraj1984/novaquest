"""1.24.0: maths books: powers and fractions read from the PDF, maths chapter titles and headings, maths questions and
exam papers without Claude, and maths-aware instructions for Claude."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from novaquest.services import books, exams, ingest, institutions, maths, outline, pdfs, questions
from tests.base import NQTestCase

BODY = ("We know that the area of a square is the product of its sides. Squares of numbers appear in many patterns "
        "and they help us to find areas quickly. Look at the table and find the pattern in the numbers below.")


def maths_pdf(path: Path) -> Path:
    """A two-page maths chapter: a title over two lines in capitals, numbered sections, a power printed small and
    raised, a stacked fraction inside a line of working, and bold headings."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4

    def text(x, y, s, size=12, font="Helvetica"):
        c.setFont(font, size)
        c.drawString(x, y, s)
        return x + c.stringWidth(s, font, size)

    y = h - 80
    text(60, y, "1", 40)
    text(60, y - 50, "A SQUARE", 22, "Helvetica-Bold")
    text(60, y - 76, "AND A CUBE", 22, "Helvetica-Bold")
    y -= 130
    text(60, y, "1.1 Square Numbers", 14, "Helvetica-Bold")
    for i, line in enumerate([BODY[:95], BODY[95:]]):
        text(60, y - 20 - 16 * i, line)
    y -= 80
    x = text(60, y, "We write 3 × 3 = 3")
    x = text(x, y + 5, "2", 7)
    text(x + 1, y, " = 9, and 4 × 4 = 16.")
    y -= 40
    x = text(60, y, "So 561.9 = (5 × 100) + (6 × 10) + 1 + (9 ×")
    text(x + 4, y + 7, "1", 12)
    c.line(x + 3, y + 4, x + 17, y + 4)
    text(x + 4, y - 7, "10", 12)
    text(x + 22, y, ").")
    y -= 50
    text(60, y, "1.2 Cubic Numbers", 14, "Helvetica-Bold")
    text(60, y - 20, "A cube has a volume equal to the product of its three sides. " + BODY[:40])
    text(60, y - 36, BODY[40:130])
    c.showPage()
    y = h - 80
    text(60, y, "Triangles", 13, "Helvetica-Bold")
    text(60, y - 20, "The area of a triangle is half the base times the height. " + BODY[:50])
    text(60, y - 36, BODY[50:140])
    text(60, y - 80, "Figure it Out", 13, "Helvetica-Bold")
    text(60, y - 100, "Find the area of each triangle drawn on the grid. " + BODY[:40])
    text(60, y - 140, "Parallelogram", 13, "Helvetica-Bold")
    text(60, y - 160, "A parallelogram has two pairs of parallel sides. " + BODY[:60])
    text(60, y - 176, BODY[60:150])
    c.showPage()
    c.save()
    return path


@unittest.skipUnless(shutil.which("pdftotext") and shutil.which("pdftohtml"), "needs Poppler")
class ReadingMathsPdfs(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pdf = maths_pdf(self.dir / "ch1.pdf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_powers_and_fractions(self):
        """[MB-1] A maths book keeps its maths: a small raised 2 after 3 is read as 3² (plain reading gave '32'), and a
        1 printed over a 10 inside a line of working is read as 1/10 in that line (plain reading lost it). Other books
        are read exactly as before."""
        plain = "\n".join(ingest.extract_pages(self.pdf))
        mathsy = "\n".join(ingest.extract_pages(self.pdf, maths=True))
        self.assertIn("3 × 3 = 32", plain.replace("3 2", "32"))
        self.assertIn("3 × 3 = 3²", mathsy)
        self.assertIn("(9 × 1/10", mathsy)
        self.assertNotIn("1/10", plain)
        self.assertIn("561.9 = (5 × 100) + (6 × 10) + 1", mathsy)

    def test_titles_and_bold_headings(self):
        """[MB-2] A chapter title printed over two lines in capitals is joined ('A Square and a Cube'); a hyphen at a
        line break joins ('Baudhāyana-Pythagoras'); a figure label isn't glued onto a heading ('Kite and Trapezium');
        bold headings ('Triangles', 'Parallelogram', but not 'Figure it Out') are found for chapters that don't number
        all their sections."""
        pages = ingest.extract_pages(self.pdf, maths=True)
        ch = outline.file_chapters([("ch1.pdf", pages)])[0]
        self.assertEqual(ch.title, "A Square and a Cube")
        self.assertEqual(outline.tidy_title("THE BAUDHĀYANA- PYTHAGORAS THEOREM"), "The Baudhāyana-Pythagoras Theorem")
        self.assertEqual(outline.tidy_title("pre- and post-war"), "pre- and post-war")
        heads = ingest.bold_headings(self.pdf)
        self.assertIn("Triangles", heads)
        self.assertIn("Parallelogram", heads)
        self.assertNotIn("Figure it Out", heads)
        kite = outline.Chapter(4, "Quadrilaterals", [(1, "4.5 Playing with Quadrilaterals"), (1, BODY), (2, "4.6 Kite and Trapezium"),
                                                     (2, "Kite"), (2, BODY)])
        self.assertEqual([t.title for t in outline.find_topics(kite)], ["Playing with Quadrilaterals", "Kite and Trapezium"])
        one = outline.Chapter(7, "Area", [(1, "7.1 Rectangle and Squares")] + [(1, BODY)] * 10 + [(2, "Triangles")]
                              + [(2, BODY)] * 10 + [(3, "Parallelogram")] + [(3, BODY)] * 10, headings=("Triangles", "Parallelogram"))
        self.assertEqual([t.title for t in outline.find_topics(one)], ["Rectangle and Squares", "Triangles", "Parallelogram"])


class MathsQuestionsOffline(NQTestCase):
    def _maths_book(self):
        school = self.make_school(n_students=1)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pdf = maths_pdf(tmp / "ch1.pdf")
        with open(pdf, "rb") as fh:
            sid = books.create(self.db, school.admin, board="NCERT", subject="Mathematics", grade="8", title="Maths 8",
                               files=[("ch1.pdf", fh)], storage=tmp / "store")
        books.read_book(self.db, self.engine, school.inst, sid, tmp / "store")
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id,
                                        syllabus_id=sid)
            topics = {r["title"]: r["id"] for r in tx.all("SELECT id, title FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic'", [sid])}
            passage = tx.val("""SELECT string_agg(p.body, ' ') FROM source_passages p JOIN syllabus_nodes n ON n.id=p.node_id
                                WHERE n.syllabus_id=%s""", [sid])
        return school, sid, topics, passage

    def test_maths_book_questions_without_claude(self):
        """[MB-3][MA-4] A maths book is read with its maths (3² is stored as 3²), and without Claude its quizzes,
        short answers and long answers are worked maths problems from NovaQuest's ready-made set that fit each topic
        (not sentences from the book): multiple choice whose wrong options are typical slips, and 'show your working'
        questions marked on the method's steps."""
        school, sid, topics, passage = self._maths_book()
        self.assertIn("3²", passage)
        self.assertIn("Square Numbers", topics)
        tid = topics["Square Numbers"]
        quiz = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id, topic_ids=[tid],
                                             kind="quiz", difficulty="medium", count=3, title="Squares")
        short = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id, topic_ids=[tid],
                                              kind="short", difficulty="medium", count=2, title="Squares working")
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, quiz)
            ss = questions.assignment_questions(tx, short)
        for q in qs:
            self.assertFalse(q["prompt"].startswith("Which statement"), q["prompt"])
            self.assertEqual(len(q["options"]), 4)
            self.assertTrue(all(any(ch.isdigit() for ch in o) for o in q["options"]), q["options"])
        sq = next(q for q in qs if q["prompt"].startswith("What is ") and "²" in q["prompt"])
        n = int(sq["prompt"].split("What is ")[1].split("²")[0])
        self.assertEqual(sq["options"][sq["answer"]["index"]], str(n * n))
        self.assertIn(str(2 * n), sq["options"], "2 × n: the usual slip for n²")
        for q in ss:
            self.assertTrue(q["prompt"].endswith("Show your working."))
            self.assertTrue(q["rubric"][-1]["point"].startswith("Correct answer:"))

    def test_maths_exam_paper(self):
        """[MB-4][AS-8] In a maths exam paper, 2 to 5 mark sections default to number problems (show working); the
        answer key gives the worked solution and shares the marks between the method's steps and the final answer;
        powers print raised in the PDF (the standard PDF font has no ² or ⁵)."""
        school, sid, topics, _ = self._maths_book()
        pattern = exams.parse_pattern([(4, 1, ""), (2, 3, ""), (1, 6, "")], maths=True)
        self.assertEqual([s["kind"] for s in pattern], ["mcq", "numeric", "long"])
        self.assertEqual(exams.parse_pattern([(2, 3, "")])[0]["kind"], "short", "other subjects: unchanged")
        made = exams.create(self.db, self.engine, school.teacher, class_id=school.class_id, syllabus_id=sid, title="Unit test",
                            topic_ids=list(topics.values()), pattern=pattern, difficulty="medium", duration_min=40)
        with self.db.tenant(school.inst) as tx:
            v = exams.view(tx, school.teacher, made["id"])
        long = v["sets"][0]["sections"][2]["items"][0]
        self.assertRegex(long["parts"][0], r"\d.*\? Which rule or method do you need to solve this, and why\?$",
                         "the problem is in part (a), before the question about its method")
        self.assertEqual(long["parts"][1], "Now solve it, showing your working.")
        sec = v["sets"][0]["sections"][1]
        item = sec["items"][0]
        self.assertTrue(item["model"])
        self.assertTrue(item["scheme"][-1][0].startswith("Correct final answer:"))
        self.assertEqual(sum(m for _, m in item["scheme"]), 3)
        key = pdfs.exam_paper(v, 1, "SUN School", key=True)
        self.assertTrue(key.startswith(b"%PDF"))
        self.assertEqual(pdfs._safe("What is 2⁵ × 10⁻³?"), "What is 2<super>5</super> × 10<super>-3</super>?")

    def test_library_mcqs_are_sound(self):
        """[MB-3] Every ready-made problem, as multiple choice for many students' numbers, has four different options
        and its right answer among them."""
        for t in maths.LIBRARY:
            made = 0
            for seed in range(20):
                q = maths.mcq_for(t, seed)
                if q:
                    made += 1
                    self.assertEqual(len(set(q["options"])), 4, q)
                    k = maths._key_from(t, sample=seed)
                    self.assertEqual(q["options"][q["answer"]], maths.show(maths.instance(k, seed)["value"], k.get("unit") or "",
                                                                            k.get("decimals")))
            self.assertGreater(made, 15, t["prompt"])


class ClaudeMaths(NQTestCase):
    def test_maths_instructions(self):
        """[MB-5][NF-AI] For a maths course Claude is told to test reasoning and calculation, with wrong options from
        typical mistakes and worked solutions as model answers; for science it isn't. Marking instructions give credit
        for correct method steps."""
        from novaquest.ai import claude as cl
        from novaquest.ai.provider import ClaudeProvider
        from novaquest.ai.types import TopicCtx
        from tests.test_ai import FakeServer, claude_reply

        server = FakeServer(lambda b, h: claude_reply(json.dumps({"questions": []})))
        try:
            eng = cl.ClaudeEngine(ClaudeProvider("k", server.url, "f", "s", timeout_s=2, max_retries=0))
            t = TopicCtx(id=1, code="1.1", title="Square Numbers", outcome="o", terms=(), chapter_no=1, chapter_title="Squares",
                         ai_concept=None, passages=({"id": 1, "ref": "p1", "body": "3² = 9"},), course="Grade 8 Mathematics (NCERT)")
            eng.generate_questions([t], "mcq", "medium", 1)
        finally:
            server.close()
        self.assertIn("MATHEMATICS", server.calls[0]["body"]["system"][0]["text"])
        self.assertNotIn("MATHEMATICS", cl._GEN)
        self.assertIn("method step", cl._MARK)


class ContentsTitles(unittest.TestCase):
    PART1 = ["About the Book\nThe Chapter 1: ‘A Square and A Cube’ is an introduction.\n",
             "CONTENTS\nForeword iii\nChapter 1\nA Square and A Cube\n1\nChapter 2\nPower Play\n19\n"
             "Chapter 3\nProportional Reasoning-1\n48\n"]
    PART2 = ["About the Book\nChapter 1: ‘Fractions in Disguise’ introduces percentages and several\napplications.\n",
             "CONTENTS\nChapter 1\nFractions in Disguise\n1\nChapter 2\nThe Baudhayana-Pythagoras Theorem\n33\n"
             "Chapter 3\nArea\n90\n"]

    @staticmethod
    def _chapter(no, *title_lines, body="We know that the area of a square is the product of its sides and more words here."):
        return [f"{no}\n" + "\n".join(title_lines) + f"\n{no}.1 First Section\n{body}\n{no}.2 Second Section\n{body}\n"]

    def test_titles_match_their_own_chapters(self):
        """[MB-2] The contents page is read from its 'Contents' heading on (not the sentences in 'About the Book'), a
        title like 'Proportional Reasoning-1' keeps its '-1', and each contents title goes to the chapter whose first
        page prints it: a two-part book uploaded as one, or one part with the other part's front matter, no longer
        gets the other part's titles by chapter number. Matching is by whole words ('Area' isn't found in 'square
        and')."""
        self.assertEqual(outline.contents_titles(self.PART2), {1: "Fractions in Disguise", 2: "The Baudhayana-Pythagoras Theorem",
                                                               3: "Area"})
        self.assertEqual(outline.contents_titles(self.PART1)[3], "Proportional Reasoning-1")
        p1 = [("hegp101.pdf", self._chapter(1, "A SQUARE", "AND A CUBE")), ("hegp102.pdf", self._chapter(2, "POWER PLAY")),
              ("hegp103.pdf", self._chapter(3, "PROPORTIONAL", "REASONING-1"))]
        p2 = [("hegp201.pdf", self._chapter(1, "FRACTIONS IN", "DISGUISE")),
              ("hegp202.pdf", self._chapter(2, "THE BAUDHĀYANAPYTHAGORAS THEOREM")), ("hegp203.pdf", self._chapter(3, "AREA"))]
        both = outline.file_chapters(p1 + p2 + [("hegp1ps.pdf", self.PART1), ("hegp2ps.pdf", self.PART2)])
        self.assertEqual([c.title for c in both], ["A Square and A Cube", "Power Play", "Proportional Reasoning-1",
                                                   "Fractions in Disguise", "The Baudhayana-Pythagoras Theorem", "Area"])
        wrong = outline.file_chapters(p1 + [("hegp2ps.pdf", self.PART2)])
        self.assertEqual([c.title for c in wrong], ["A Square and a Cube", "Power Play", "Proportional Reasoning-1"])
