"""1.10.0: exam papers (AS-8). A teacher sets the pattern (sections of n questions at m marks), the difficulty and
the topics; NovaQuest writes three different papers (sets A, B, C) to compare, the teacher swaps any question, chooses
one, and downloads it as a printable PDF with a separate answer key and marking scheme."""
from __future__ import annotations

import asyncio
import io
import re

import pypdf

from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import exams, pdfs
from tests.base import PASSWORD, NQTestCase


def _text(pdf: bytes) -> tuple[str, int]:
    r = pypdf.PdfReader(io.BytesIO(pdf))
    return "\n".join(p.extract_text() for p in r.pages), len(r.pages)


class ExamCase(NQTestCase):
    def paper(self, school, pattern, *, chapters=("8.", "11."), difficulty="mixed", answer_space=False):
        tops = [v for k, v in school.topics.items() if k.startswith(chapters)]
        made = exams.create(self.db, self.engine, school.teacher, class_id=school.class_id, syllabus_id=school.syllabus_id,
                            title="Unit Test 2", topic_ids=tops, pattern=exams.parse_pattern(pattern), difficulty=difficulty,
                            duration_min=90, instructions="Draw neat diagrams wherever needed.", answer_space=answer_space)
        with self.db.tenant(school.inst) as tx:
            return made, exams.view(tx, school.teacher, made["id"])


class Pattern(ExamCase):
    def test_pattern_rules(self):
        """[AS-8] A pattern is up to 6 sections of 1-50 questions (60 in all) at ½ to 10 marks. Blank rows are skipped.
        The kind follows the marks unless chosen: 1 mark multiple choice, 2-4 a short answer, 5+ a long answer.
        Multiple choice carries at most 2 marks. Written answers' marks are shared between marking points in halves."""
        p = exams.parse_pattern([(10, 1, ""), ("", "", ""), (10, 2, ""), (5, 5, "")])
        self.assertEqual([(s["name"], s["count"], s["marks"], s["kind"]) for s in p],
                         [("A", 10, 1, "mcq"), ("B", 10, 2, "short"), ("C", 5, 5, "long")])
        self.assertEqual(exams.total_marks(p), 55)
        self.assertEqual(exams.parse_pattern([(4, 2, "mcq")])[0]["kind"], "mcq")
        self.assertEqual([exams.total_marks(exams.parse_pattern([(c, m, "") for c, m in rows])) for _, rows in exams.PRESETS],
                         [25, 25, 50, 55, 80], "each preset's label states its real total")
        for label, _ in exams.PRESETS:
            self.assertRegex(label, r"\(\d+ marks\)")
        for bad, msg in (([], "at least one section"), ([(5, 3, "mcq")], "at most 2 marks"), ([(5, 7, "")], "Marks per question"),
                         ([(51, 1, "")], "1 to 50"), ([(30, 1, ""), (31, 1, "")], "at most 60"), ([("x", 1, "")], "like 10 and 1"),
                         ([(1, 1, "")] * 7, "at most 6 sections")):
            with self.assertRaises(ValidationError, msg=bad) as cm:
                exams.parse_pattern(bad)
            self.assertIn(msg, cm.exception.message)
        self.assertEqual(exams.split_marks(5, 3), [2, 1.5, 1.5])
        self.assertEqual(exams.split_marks(3, 2), [1.5, 1.5])
        self.assertEqual(sum(exams.split_marks(5, 4)), 5)
        same = [_Q({"kind": "mcq", "prompt": "Fill in the blank: In science, a push or a _____ on an object is called a force.",
                    "answer": {"text": "pull"}}),
                _Q({"kind": "mcq", "prompt": "Fill in the blank: In science, a push or a pull on an object is called a _____.",
                    "answer": {"text": "force"}})]
        self.assertEqual(exams._core(same[0]), exams._core(same[1]), "one sentence with different words blanked is one question")
        pick = _Q({"kind": "mcq", "prompt": "Which statement about a force is correct?",
                   "answer": {"text": "In science, a push or a pull on an object is called a force."}})
        self.assertEqual(exams._core(pick), exams._core(same[0]), "...and so is the same sentence as a statement to pick")
        true = _Q({"kind": "mcq", "prompt": "Which statement about cells is correct?", "answer": {
            "text": "Organisms made of more than one cell are called multicellular (multi: many; cellular: cell) organisms."}})
        swapped = _Q({"kind": "mcq", "prompt": "Which statement about cells is NOT correct?", "answer": {
            "text": "Organisms made of more than one cell are called unicellular (multi: many; cellular: cell) organisms."}})
        self.assertEqual(exams._core(swapped), exams._core(true), "...or with a word swapped as the false one")
        self.assertEqual((exams.fmt_marks(0.5), exams.fmt_marks(2), exams.fmt_marks(1.5)), ("½", "2", "1½"))


class ThreeSets(ExamCase):
    def test_three_papers_follow_the_pattern_and_difficulty(self):
        """[AS-8] Three sets, each exactly to the pattern (number, marks and kind of question per section), never
        repeating a question (or the same sentence with another word blanked) within a set. The sets are different
        papers, and the number of questions they share is reported. A mixed paper is 30/50/20 easy/medium/hard per
        section; a 'hard' paper is all hard."""
        school = self.make_school(n_students=2)
        made, v = self.paper(school, [(10, 1, ""), (10, 2, ""), (5, 5, "")])
        self.assertEqual((v["total"], v["questions"], len(v["sets"])), (55, 25, 3))
        for s in v["sets"]:
            self.assertEqual([(len(sec["items"]), sec["kind"]) for sec in s["sections"]], [(10, "mcq"), (10, "short"), (5, "long")])
            items = [q for sec in s["sections"] for q in sec["items"]]
            self.assertEqual([q["no"] for q in items], list(range(1, 26)), "questions are numbered through the paper")
            self.assertEqual(sum(q["marks"] for q in items), 55)
            cores = [exams._core(_Q(q)) for q in items]
            self.assertEqual(len(cores), len(set(cores)), f"Set {s['name']} repeats a question")
            self.assertTrue(all(q["kind"] == "mcq" and len(q["options"]) == 4 and q["letter"] for q in s["sections"][0]["items"]))
            self.assertTrue(all(q["kind"] == "viva" and q["parts"] and q["scheme"] for q in s["sections"][2]["items"]))
            for q in s["sections"][1]["items"]:
                self.assertEqual(sum(m for _, m in q["scheme"]), 2, "a 2-mark answer's points add up to 2")
            for sec in s["sections"]:
                got = {d: sum(1 for q in sec["items"] if q["difficulty"] == d) for d in ("easy", "medium", "hard")}
                self.assertEqual(got, {d: n for d, n in exams._difficulty_counts("mixed", sec["count"])} | {
                    d: 0 for d in ("easy", "medium", "hard") if d not in dict(exams._difficulty_counts("mixed", sec["count"]))})
            self.assertEqual(sum(m for _, m in s["chapters"]), 55)
            self.assertLessEqual({c.split(".")[0] for c, _ in s["chapters"]}, {"8", "11"})
        a, b, c = (s["ids"] for s in v["sets"])
        self.assertLess(len(a & b), 25 // 2, "Sets A and B are mostly different questions")
        self.assertLess(len(a & c), 25 // 2)
        shared = len((a & b) | (a & c) | (b & c))
        self.assertEqual(made["shared"], shared)
        self.assertEqual(v["sets"][0]["shared"], len(a & (b | c)))

        _, hard = self.paper(school, [(10, 1, "")], chapters=("8.",), difficulty="hard")
        self.assertEqual({q["difficulty"] for s in hard["sets"] for q in s["sections"][0]["items"]}, {"hard"})

    def test_choose_and_replace(self):
        """[AS-8] Choosing a set approves its questions (so they join the question bank); the other sets stay drafts.
        Replace swaps one question for a new one of the same kind and marks that is nowhere in the paper. Another
        school's teacher can't see or change the paper."""
        school = self.make_school(n_students=2)
        made, v = self.paper(school, [(5, 1, ""), (3, 3, "")], chapters=("8.",))
        pid = made["id"]
        old = v["sets"][1]["sections"][1]["items"][0]
        new_id = exams.replace(self.db, self.engine, school.teacher, pid, 2, 2, 1)
        with self.db.tenant(school.inst) as tx:
            v2 = exams.view(tx, school.teacher, pid)
            q = v2["sets"][1]["sections"][1]["items"][0]
            self.assertEqual((q["id"], q["kind"], q["marks"], q["no"]), (new_id, old["kind"], 3, 6))
            self.assertNotEqual(q["prompt"], old["prompt"])
            others = [exams._core(_Q(x)) for s in v2["sets"] for sec in s["sections"] for x in sec["items"] if x["id"] != new_id]
            self.assertNotIn(exams._core(_Q(q)), others)
            exams.choose(tx, school.teacher, pid, 2)
            self.assertEqual(tx.val("""SELECT count(*) FROM exam_paper_items i JOIN questions q ON q.id=i.question_id
                                       WHERE i.paper_id=%s AND i.set_no=2 AND q.status <> 'approved'""", [pid]), 0)
            self.assertGreater(tx.val("""SELECT count(*) FROM exam_paper_items i JOIN questions q ON q.id=i.question_id
                                         WHERE i.paper_id=%s AND i.set_no=1 AND q.status='draft'""", [pid]), 0)
            self.assertEqual(exams.view(tx, school.teacher, pid)["paper"]["chosen_set"], 2)
            with self.assertRaises(ValidationError):
                exams.choose(tx, school.teacher, pid, 4)
        other = self.make_school(code="MOON", n_students=1)
        with self.db.tenant(other.inst) as tx:
            with self.assertRaises(NotFound):
                exams.view(tx, other.teacher, pid)
            self.assertEqual(exams.list_papers(tx, other.teacher), [])


class Printing(ExamCase):
    def test_question_paper_and_answer_key_pdfs(self):
        """[AS-8] The question paper prints on A4 with the school, title, class and subject, set, time allowed and
        maximum marks; name, roll number and date lines; general instructions (with the teacher's own); 'SECTION A -
        10 x 1 = 10 marks' headings; each question's marks; and 'Page x of y'. No answers appear on it. The answer key
        has every multiple-choice answer and each written answer's marking points. With answer space on, students get
        lines to write on."""
        school = self.make_school(n_students=2)
        _, v = self.paper(school, [(10, 1, ""), (10, 2, ""), (5, 5, "")])
        text, pages = _text(pdfs.exam_paper(v, 1, "SUN School, Coimbatore"))
        flat = re.sub(r"\s+", " ", text)
        for want in ("SUN School, Coimbatore", "Unit Test 2", "Class 8B", "Set A", "Time allowed: 1½ hours",
                     "Maximum marks: 55", "Name:", "Roll no.:", "All questions are compulsory.", "Draw neat diagrams wherever needed.",
                     "SECTION A", "10 × 1 = 10 marks", "SECTION B", "10 × 2 = 20 marks", "SECTION C", "5 × 5 = 25 marks",
                     f"Page 1 of {pages}"):
            self.assertIn(want, flat, want)
        self.assertNotIn("Answer:", flat)
        self.assertNotIn("✓", flat)
        self.assertGreaterEqual(pages, 2)
        q1 = v["sets"][0]["sections"][0]["items"][0]
        self.assertIn(re.sub(r"\s+", " ", q1["prompt"])[-25:].replace("____", "").strip()[:12], flat)

        key, _ = _text(pdfs.exam_paper(v, 1, "SUN School, Coimbatore", key=True))
        key = re.sub(r"\s+", " ", key)
        self.assertIn("Answer key and marking scheme", key)
        self.assertNotIn("Name: ____", key)
        self.assertEqual(key.count("Answer: ("), 10, "every multiple-choice answer is in the key")
        self.assertIn(f"Answer: ({q1['letter']})", key)

        b, _ = _text(pdfs.exam_paper(v, 2, "SUN School, Coimbatore"))
        self.assertIn("Set B", b)
        _, spaced = self.paper(school, [(10, 1, ""), (10, 2, ""), (5, 5, "")], answer_space=True)
        self.assertGreater(_text(pdfs.exam_paper(spaced, 1, "SUN School"))[1], pages, "answer lines add pages")


class Screens(ExamCase):
    def test_teacher_builds_chooses_and_downloads_over_http(self):
        """[AS-8][UX-1] From Assignments and Home a teacher opens Exam papers, fills the form (a preset or their own
        pattern), gets three sets to compare, replaces a question, chooses one and downloads the paper and the answer
        key as PDFs. A bad pattern or no topics gives a plain message and keeps the form. Another school's teacher
        gets 'not found' for the paper and its PDF."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        self.make_school(code="MOON", n_students=1)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertIn('href="/teacher/papers"', t.get("/teacher/assignments")[1])
            self.assertIn('href="/teacher/papers/new', t.get("/teacher/assignments/new")[1])  # 1.30.0: from New assignment
            self.assertIn("No exam papers yet.", t.get("/teacher/papers")[1])
            html = t.get("/teacher/papers/new")[1]
            for p in exams.PRESETS:
                self.assertIn(p[0], html)
            ch8 = [str(v) for k, v in school.topics.items() if k.startswith("8.")]
            status, html, _, _ = t.post("/teacher/papers/new", {"pattern": "custom", "count_0": "5", "marks_0": "3", "kind_0": "mcq",
                                                                "difficulty": "mixed", "title": "Unit Test", "topics[]": ch8})
            self.assertEqual(status, 422)
            self.assertIn("at most 2 marks", html)
            self.assertIn('<option value="3" selected>', html, "the form keeps what was entered")
            status, html, _, _ = t.post("/teacher/papers/new", {"pattern": "1", "difficulty": "easy", "title": "Unit Test"})
            self.assertEqual(status, 422)
            self.assertIn("Choose at least one topic.", html)
            status, html, _, url = t.post("/teacher/papers/new", {"pattern": "custom", "count_0": "10", "marks_0": "1", "count_1": "5",
                                                                  "marks_1": "3", "difficulty": "mixed", "title": "Unit Test 1",
                                                                  "duration": "60", "topics[]": ch8, "answer_space": "1"})
            self.assertEqual(status, 200)
            pid = int(re.search(r"/teacher/papers/(\d+)", url).group(1))
            self.assertIn("Three options are ready", html)
            self.assertIn("25 marks", html)
            self.assertEqual(len(re.findall(r'aria-label="Replace question \d+"', html)), 15)
            for n in (1, 2, 3):
                self.assertIn(f'href="/teacher/papers/{pid}?set={n}"', html)
            self.assertIn("Choose Set A", t.get(f"/teacher/papers/{pid}?set=1")[1])
            _, html, _, _ = t.post(f"/teacher/papers/{pid}/replace", {"set": "3", "section": "2", "item": "4"})
            self.assertIn("Question replaced.", html)
            _, html, _, _ = t.post(f"/teacher/papers/{pid}/choose", {"set": "3"})
            self.assertIn("Set C is your chosen paper.", html)
            self.assertIn(f'href="/teacher/papers/{pid}/set-3.pdf"', html)
            self.assertIn("Set C chosen", t.get("/teacher/papers")[1])
            status, _, headers, _ = t.get(f"/teacher/papers/{pid}/set-3.pdf")
            self.assertEqual((status, headers["content-type"]), (200, "application/pdf"))
            self.assertIn("unit-test-1-set-c.pdf", headers["content-disposition"])
            self.assertIn("Set C", _text(t.last_bytes)[0])
            status, _, headers, _ = t.get(f"/teacher/papers/{pid}/set-3-key.pdf")
            self.assertIn("answer-key", headers["content-disposition"])
            self.assertIn("Answer key and marking scheme", re.sub(r"\s+", " ", _text(t.last_bytes)[0]))
            self.assertEqual(t.get(f"/teacher/papers/{pid}/set-4.pdf")[0], 404)
            m = Client(live.url)
            m.login("rao@moon.test", PASSWORD)
            self.assertEqual(m.get(f"/teacher/papers/{pid}")[0], 404)
            self.assertEqual(m.get(f"/teacher/papers/{pid}/set-1.pdf")[0], 404)
            self.assertEqual(m.post(f"/teacher/papers/{pid}/choose", {"set": "1"})[0], 404)
        finally:
            live.stop()

    def test_exam_screens_on_a_phone(self):
        """[AS-8][NF-A11Y] On a phone the form's own-pattern table shows a running total, 'All topics in this chapter'
        ticks a chapter, and the paper page puts the set summary and Choose button above the questions. No
        accessibility problems and nothing wider than the screen."""
        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS, PHONE, SHOTS

        self.make_school(n_students=2)
        live = LiveServer(self.settings, self.db)
        SHOTS.mkdir(parents=True, exist_ok=True)

        async def go():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                page = await (await b.new_context(**PHONE)).new_page()
                await page.goto(live.url + "/login")
                await page.fill("#email", "rao@sun.test")
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_load_state()
                out = {"problems": {}}
                await page.goto(live.url + "/teacher/papers/new")
                out["problems"]["new"] = await page.evaluate(A11Y_JS)
                await page.click("text=My own pattern (below)")
                await page.click(".custom-pattern summary")
                await page.fill("#count_2", "2")
                await page.select_option("#marks_2", "5")
                out["total"] = await page.inner_text("[data-exam-total]")
                await page.locator("[data-topic-tree] summary").nth(7).click()
                await page.locator("[data-check-all]").nth(7).check()
                out["count"] = await page.inner_text("[data-topic-count]")
                out["wide_new"] = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
                await page.screenshot(path=str(SHOTS / "phone_exam_new.png"), full_page=True)
                await page.click("button:has-text('Write three papers')")
                await page.wait_for_load_state()
                out["problems"]["paper"] = await page.evaluate(A11Y_JS)
                out["wide_paper"] = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
                out["order"] = await page.evaluate("""() => document.querySelector('.paper-cols > .aside').getBoundingClientRect().top
                                                      < document.querySelector('.paper-cols > .main').getBoundingClientRect().top""")
                await page.screenshot(path=str(SHOTS / "phone_exam_paper.png"))
                await page.goto(live.url + "/teacher/papers")
                out["problems"]["list"] = await page.evaluate(A11Y_JS)
                await b.close()
                return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        self.assertEqual(out["problems"], {"new": [], "paper": [], "list": []})
        self.assertEqual(out["total"], "Total: 17 questions, 35 marks")
        self.assertEqual(out["count"], "6 chosen")
        self.assertEqual((out["wide_new"], out["wide_paper"]), (0, 0))
        self.assertTrue(out["order"], "the set summary and Choose come first on a phone")


class _Q:
    """A question row from exams.view, shaped like an engine question for exams._core."""

    def __init__(self, r: dict) -> None:
        self.kind, self.prompt, self.answer = r["kind"], r["prompt"], r["answer"]
