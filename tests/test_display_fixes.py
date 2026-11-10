"""1.10.1: three display fixes from reviewing the screenshots: the projector's answer and class result sit above the
control bar, an exam set's marks by chapter are in book order, and the results' AI skills tile says why it is empty."""
from __future__ import annotations

import asyncio

from novaquest.services import attempts, exams, review
from tests.base import PASSWORD, NQTestCase


class Projector(NQTestCase):
    def test_answer_and_class_result_are_never_under_the_control_bar(self):
        """[AS-7][UX-1] On a laptop (1366 x 900) and a typical projector (1280 x 720), revealing each question's answer
        keeps the answer line and '..% of the class got this right' fully visible above the bottom control bar, and
        the highlighted option is not repeated in the answer line."""
        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS, SHOTS

        school = self.make_school(n_students=5)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=6)
        for i in range(5):
            st = school.student(i)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=i % 2 == 0))
        live = LiveServer(self.settings, self.db)
        SHOTS.mkdir(parents=True, exist_ok=True)

        async def go():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                out = {}
                for name, size in (("laptop", (1366, 900)), ("projector", (1280, 720))):
                    page = await (await b.new_context(viewport={"width": size[0], "height": size[1]})).new_page()
                    await page.goto(live.url + "/login")
                    await page.fill("#email", "rao@sun.test")
                    await page.fill("#password", PASSWORD)
                    await page.click("button[type=submit]")
                    await page.wait_for_load_state()
                    await page.goto(f"{live.url}/teacher/assignments/{aid}/present")
                    hidden, stats, repeats = [], 0, 0
                    for n in range(1, 7):
                        await page.click("[data-slide-next]")
                        await page.click("[data-slide-reveal]")
                        box = await page.evaluate("""() => {
                            const s = [...document.querySelectorAll('[data-slide]')].find(x => !x.hidden);
                            const a = s.querySelector('[data-answer]').getBoundingClientRect();
                            const nav = document.querySelector('.present-nav').getBoundingClientRect();
                            const stat = s.querySelector('.present-stat');
                            const right = s.querySelector('.present-opts li.right');
                            return {bottom: a.bottom, top: a.top, nav: nav.top, stat: !!stat,
                                    repeat: !!right && s.querySelector('[data-answer]').innerText.includes(right.innerText.slice(1).trim())}; }""")
                        if box["bottom"] > box["nav"] + 0.5 or box["top"] < 0:
                            hidden.append((n, box))
                        stats += box["stat"]
                        repeats += box["repeat"]
                        if n == 2:
                            await page.screenshot(path=str(SHOTS / f"projector_{name}.png"))
                    out[name] = {"hidden": hidden, "stats": stats, "repeats": repeats, "a11y": await page.evaluate(A11Y_JS)}
                await b.close()
                return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        for name, r in out.items():
            self.assertEqual(r["hidden"], [], f"{name}: the answer went under the control bar")
            self.assertEqual(r["stats"], 6, f"{name}: every question shows the class's result")
            self.assertEqual(r["repeats"], 0, f"{name}: the highlighted option isn't repeated")
            self.assertEqual(r["a11y"], [])


class ExamChapters(NQTestCase):
    def test_marks_by_chapter_are_in_book_order(self):
        """[AS-8] An exam set's marks by chapter list chapter 8 before chapter 11 (book order, not text order)."""
        self.assertEqual(sorted(["11. Force and Pressure", "8. Cell", "2. Microorganisms", "Extra"], key=exams._chapter_order),
                         ["2. Microorganisms", "8. Cell", "11. Force and Pressure", "Extra"])
        school = self.make_school(n_students=1)
        tops = [v for k, v in school.topics.items() if k.startswith(("8.", "11."))]
        made = exams.create(self.db, self.engine, school.teacher, class_id=school.class_id, syllabus_id=school.syllabus_id,
                            title="Unit Test", topic_ids=tops, pattern=exams.parse_pattern([(10, 1, ""), (4, 3, "")]),
                            difficulty="mixed", duration_min=60, instructions="", answer_space=False)
        with self.db.tenant(school.inst) as tx:
            v = exams.view(tx, school.teacher, made["id"])
        for s in v["sets"]:
            codes = [int(c.split(".")[0]) for c, _ in s["chapters"]]
            self.assertEqual(codes, sorted(codes), f"Set {s['name']}: {codes}")
            self.assertEqual(set(codes), {8, 11})


class ResultsTiles(NQTestCase):
    def test_empty_figures_say_why(self):
        """[DB-1][NR-2][UX-1] A results tile with no figure says why: before anyone hands in, 'Shown once students
        hand in'; for a task without AI questions, the AI skills tile says 'This task has no AI questions' (it showed
        a bare dash before). A task with an AI question shows its AI figure once students hand in."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=3)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=3)
        with self.db.tenant(school.inst) as tx:
            tx.exec("""UPDATE questions SET ai_concept=NULL WHERE id IN
                       (SELECT question_id FROM assignment_questions WHERE assignment_id=%s)""", [aid])
            self.assertFalse(review.class_summary(tx, aid)["has_ai"])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertEqual(html.count("Shown once students hand in"), 1, "science, before anyone hands in")
            self.assertIn("This task has no AI questions", html)
            st = school.student(0)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid))
            html = t.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertNotIn("Shown once students hand in", html)
            self.assertIn("This task has no AI questions", html)
            with self.db.tenant(school.inst) as tx:
                qid = tx.val("SELECT question_id FROM assignment_questions WHERE assignment_id=%s LIMIT 1", [aid])
                tx.exec("UPDATE questions SET ai_concept='features' WHERE id=%s", [qid])
                s = review.class_summary(tx, aid)
            self.assertTrue(s["has_ai"])
            self.assertIsNotNone(s["ai"])
            html = t.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertNotIn("This task has no AI questions", html)
        finally:
            live.stop()
