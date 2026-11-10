"""Classroom tools from the teacher review (items 10-12): results question by question, a printable worksheet and a
projector view of any assignment, and a term plan per class and book."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from novaquest.core.errors import Forbidden, ValidationError
from novaquest.services import attempts, dashboards, mastery, plans, questions, review, syllabus, worksheets
from tests.base import PASSWORD, NQTestCase


def _chapters(tx, sid):
    return {ch["code"]: ch for ch in syllabus.get_tree(tx, sid)}


class QuestionByQuestion(NQTestCase):
    def test_breakdown_shows_share_right_common_wrong_answer_and_who(self):
        """[DB-6] For each question: the share of the class that got it right, how many chose each option, the most
        chosen wrong answer, and who got it wrong. Questions fewer than half got right are listed to teach again,
        lowest first (at most three)."""
        school = self.make_school(n_students=3)
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=4)
        for i, good in ((0, False), (1, False), (2, True)):
            st = school.student(i)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=good))
        with self.db.tenant(school.inst) as tx:
            b = review.question_breakdown(tx, school.teacher, aid)
            leader_view = review.question_breakdown(tx, school.leader, aid)
        self.assertEqual(b["submitted"], 3)
        self.assertEqual(len(leader_view["questions"]), len(b["questions"]))
        self.assertTrue(b["questions"])
        for q in b["questions"]:
            self.assertEqual(q["answered"], 3)
            self.assertEqual(q["pct_right"], 33)
            self.assertEqual(sum(o["count"] for o in q["options"]), 3)
            self.assertEqual([o["count"] for o in q["options"] if o["right"]], [1])
            self.assertEqual(q["common_wrong"]["count"], 2)
            self.assertEqual([w["name"] for w in q["wrong"]], ["Student 1", "Student 2"])
            self.assertEqual(q["wrong"][0]["said"], q["common_wrong"]["text"])
        self.assertEqual(len(b["reteach"]), min(3, len(b["questions"])))
        other = self.make_school("MOON")
        with self.db.tenant(other.inst) as tx:
            with self.assertRaises(Exception):
                review.question_breakdown(tx, other.teacher, aid)

    def test_written_answers_count_as_right_from_sixty_percent(self):
        """[DB-6] For written answers the share right counts answers with at least 60% of the marks, and the average
        share of marks is shown with the model answer."""
        school = self.make_school(n_students=2)
        aid = self.open_assignment(school, kind="short", topics=("8.3",), count=1)
        for i, good in ((0, True), (1, False)):
            st = school.student(i)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=good))
        with self.db.tenant(school.inst) as tx:
            q = review.question_breakdown(tx, school.teacher, aid)["questions"][0]
        self.assertIsNone(q["options"])
        self.assertEqual(q["pct_right"], 50)
        self.assertTrue(q["model"])
        self.assertEqual([w["name"] for w in q["wrong"]], ["Student 2"])
        self.assertIn("not sure", q["wrong"][0]["said"])


class WorksheetAndProjector(NQTestCase):
    def test_worksheet_and_projector_over_http(self):
        """[AS-7][DB-6] Over HTTP: the worksheet lists only approved questions (and says how many are still waiting),
        with name and roll lines; the answer key appears only when asked for and gives the right letter. The projector
        view has a title screen plus one screen per question with the answer hidden until shown; once students have
        handed in, each answer screen says how many got it right. The review page's By question tab lists the
        questions worth teaching again. Teachers of other schools can't open any of it."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=3)
        draft = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                              topic_ids=[school.topics["8.5"]], kind="quiz", difficulty="easy", count=3, title="Paper")
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, draft)
            for q in qs[:2]:
                questions.approve(tx, school.teacher, q["id"])
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=3)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{draft}")[1]
            self.assertIn(f"/teacher/assignments/{draft}/worksheet", html)
            self.assertIn(f"/teacher/assignments/{draft}/present", html)
            status, html, _, _ = t.get(f"/teacher/assignments/{draft}/worksheet")
            self.assertEqual(status, 200)
            self.assertEqual(html.count('class="sheet-q"'), 2)
            self.assertIn("1 question still waiting for your approval isn&#39;t included", html)
            self.assertIn("Roll no.:", html)
            self.assertNotIn("Answer key:", html)
            html = t.get(f"/teacher/assignments/{draft}/worksheet?key=1")[1]
            self.assertIn("Answer key: Paper", html)
            self.assertEqual(len(re.findall(r"<b>\([a-d]\)</b>", html)), 2)
            html = t.get(f"/teacher/assignments/{draft}/present")[1]
            self.assertEqual(html.count("data-slide hidden") + html.count('data-slide>'), 3)
            self.assertEqual(html.count("data-answer hidden"), 2)
            self.assertNotIn("of the class got this right", html)
            for i, good in ((0, False), (1, False), (2, True)):
                st = school.student(i)
                attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=good))
            html = t.get(f"/teacher/assignments/{aid}/present")[1]
            self.assertEqual(html.count("33% of the class got this right"), 3)
            html = t.get(f"/teacher/assignments/{aid}/review?tab=questions")[1]
            self.assertIn("Worth teaching again", html)
            self.assertIn("Most common wrong answer", html)
            self.assertIn("Who didn't get it (2)", html)
            self.assertIn(f"/teacher/assignments/new?for={school.class_id}:{school.syllabus_id}&amp;topics={school.topics['8.3']}", html)
            other = self.make_school("MOON")
            o = Client(live.url)
            o.login("rao@moon.test", PASSWORD)
            for path in (f"/teacher/assignments/{aid}/worksheet", f"/teacher/assignments/{aid}/present"):
                self.assertIn(o.get(path)[0], (403, 404), path)
            with self.db.tenant(other.inst) as tx:
                with self.assertRaises(Exception):
                    worksheets.sheet(tx, other.teacher, aid)
        finally:
            live.stop()

    def test_projector_moves_with_keys_and_reveals_the_answer(self):
        """[AS-7][NF-A11Y] In a real browser on a laptop: arrow keys move between screens, 'A' shows the answer (and
        marks the right option), the counter follows, and the worksheet has no accessibility problems."""
        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS, LAPTOP, SHOTS

        school = self.make_school(n_students=1)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=3)
        live = LiveServer(self.settings, self.db)
        SHOTS.mkdir(parents=True, exist_ok=True)

        async def go():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                page = await (await b.new_context(**LAPTOP)).new_page()
                await page.goto(live.url + "/login")
                await page.fill("#email", "rao@sun.test")
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_load_state()
                await page.goto(live.url + f"/teacher/assignments/{aid}/worksheet?key=1")
                sheet_problems = await page.evaluate(A11Y_JS)
                await page.screenshot(path=str(SHOTS / "worksheet.png"), full_page=True)
                await page.goto(live.url + f"/teacher/assignments/{aid}/present")
                count = page.locator("[data-slide-count]")
                first = await count.text_content()
                await page.keyboard.press("ArrowRight")
                second = await count.text_content()
                visible = await page.locator("[data-slide]:not([hidden]) .present-q").text_content()
                hidden_before = await page.locator("[data-slide]:not([hidden]) [data-answer]").is_hidden()
                await page.keyboard.press("a")
                shown = await page.locator("[data-slide]:not([hidden]) [data-answer]").is_visible()
                right = await page.locator("[data-slide]:not([hidden]).revealed li.right").count()
                await page.screenshot(path=str(SHOTS / "projector.png"))
                await page.keyboard.press("ArrowLeft")
                back = await count.text_content()
                await b.close()
                return sheet_problems, first, second, visible, hidden_before, shown, right, back

        try:
            problems, first, second, visible, hidden_before, shown, right, back = asyncio.run(go())
        finally:
            live.stop()
        self.assertEqual(problems, [])
        self.assertEqual((first, second, back), ("1 / 4", "2 / 4", "1 / 4"))
        self.assertTrue(visible)
        self.assertTrue(hidden_before)
        self.assertTrue(shown)
        self.assertEqual(right, 1)


class TermPlan(NQTestCase):
    def test_spread_over_the_term_in_book_order_by_topic_count(self):
        """[DB-7] 'Fill in the dates for me' spreads every chapter over the term's school days (Monday to Saturday)
        in book order without overlaps, giving chapters with more topics more days; chosen chapters only, if ticked."""
        school = self.make_school()
        start = plans.today() - timedelta(days=70)
        end = plans.today() + timedelta(days=120)
        with self.db.tenant(school.inst) as tx:
            n = plans.autofill(tx, school.teacher, school.class_id, school.syllabus_id, start, end)
            v = plans.view(tx, school.teacher, school.class_id, school.syllabus_id)
        self.assertEqual(n, len(v["chapters"]))
        self.assertEqual(v["planned"], n)
        chs = v["chapters"]
        for a, b in zip(chs, chs[1:]):
            self.assertLess(a["ends_on"], b["starts_on"])
        self.assertGreaterEqual(chs[0]["starts_on"], start)
        self.assertLessEqual(chs[-1]["ends_on"], end)
        self.assertTrue(all(c["starts_on"].weekday() != 6 and c["ends_on"].weekday() != 6 for c in chs))
        days = {c["code"]: len(plans.school_days(c["starts_on"], c["ends_on"])) for c in chs}
        big = max(chs, key=lambda c: c["topics"])
        small = min(chs, key=lambda c: c["topics"])
        self.assertGreaterEqual(days[big["code"]], days[small["code"]])
        self.assertLessEqual(len(v["now"]), 1)
        with self.db.tenant(school.inst) as tx:
            ids = [chs[0]["id"], chs[1]["id"]]
            plans.autofill(tx, school.teacher, school.class_id, school.syllabus_id, plans.today(), plans.today() + timedelta(days=13), ids)
            v2 = plans.view(tx, school.teacher, school.class_id, school.syllabus_id)
        self.assertEqual(v2["chapters"][0]["starts_on"], plans.school_days(plans.today(), plans.today() + timedelta(days=13))[0])
        self.assertEqual(v2["chapters"][2]["starts_on"], chs[2]["starts_on"], "chapters not ticked keep their dates")

    def test_on_track_now_and_typing_mistakes(self):
        """[DB-7][DB-2] A chapter whose dates include today is 'in class now' (and opens first in the class grid); a
        chapter whose dates have passed is 'behind' until most of the class has practised it. Half-filled dates, an end
        before the start, a wrong year and another book's chapter are refused; blank dates take a chapter out.
        School leaders can see the plan but only the class's teacher can change it."""
        school = self.make_school(n_students=2)
        today = plans.today()
        with self.db.tenant(school.inst) as tx:
            ch = _chapters(tx, school.syllabus_id)
            c1, c2 = ch["1"], ch["2"]
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id,
                       [(c1["id"], today - timedelta(days=40), today - timedelta(days=30)),
                        (c2["id"], today - timedelta(days=2), today + timedelta(days=5))])
            v = plans.view(tx, school.teacher, school.class_id, school.syllabus_id)
            self.assertEqual([c["code"] for c in v["now"]], ["2"])
            self.assertEqual([c["code"] for c in v["behind"]], ["1"])
            self.assertEqual([p["code"] for p in plans.in_class_now(tx, school.class_id)], ["2"])
            g = dashboards.class_grid(tx, school.teacher, school.class_id)
            self.assertEqual(g["chapter"]["code"], "2", "the grid opens on the chapter in class now")
            for s in school.students:
                for t in c1["topics"]:
                    mastery.record(tx, s["student_id"], t["id"], "practice", 0.8)
            v = plans.view(tx, school.teacher, school.class_id, school.syllabus_id)
            self.assertEqual(v["behind"], [])
            self.assertEqual(next(c for c in v["chapters"] if c["code"] == "1")["practised"], 100)
            home = dashboards.teacher_home(tx, school.teacher)["classes"][0]["plan"]
            self.assertEqual((home["planned"], home["behind"], [c["code"] for c in home["now"]]), (True, 0, ["2"]))
            bad = [[(c1["id"], today, None)], [(c1["id"], today, today - timedelta(days=1))], [(999999, today, today)]]
            for entries in bad:
                with self.assertRaises(ValidationError):
                    plans.save(tx, school.teacher, school.class_id, school.syllabus_id, entries)
            with self.assertRaises(ValidationError):
                plans.parse_day((today + timedelta(days=3650)).isoformat(), "Start")
            with self.assertRaises(ValidationError):
                plans.parse_day("31/10/2026", "Start")
            with self.assertRaises(ValidationError):
                plans.autofill(tx, school.teacher, school.class_id, school.syllabus_id, today, today + timedelta(days=2))
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id, [(c1["id"], None, None)])
            self.assertEqual(plans.view(tx, school.teacher, school.class_id, school.syllabus_id)["planned"], 1)
            plans.view(tx, school.leader, school.class_id, school.syllabus_id)
            with self.assertRaises(Forbidden):
                plans.save(tx, school.leader, school.class_id, school.syllabus_id, [(c1["id"], today, today)])
        other = self.make_school("MOON")
        with self.db.tenant(other.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM term_plans"), 0, "another school can't see the plan")

    def test_plan_page_and_in_class_now_for_students_over_http(self):
        """[DB-7][UX-1] Over HTTP: Term plan is in the teacher's menu; spreading the chapters over the term and typing
        dates both save; a mistake shows a plain message and keeps the page. Home shows what's in class now. A student
        sees 'In class now' on Learn with the next topic of that chapter, and the chapter opens. The plan sits under
        Teach, so the teacher's menu keeps its four items."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        today = plans.today()
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get("/teacher")[1]
            self.assertIn(f'href="/teacher/plan?for={school.class_id}:{school.syllabus_id}"', html)  # Today: "Plan the term"
            self.assertIn("No term plan yet", html)
            self.assertIn(f'href="/teacher/plan?class={school.class_id}"', t.get("/teacher/classes")[1])
            html = t.get("/teacher/plan")[1]
            self.assertIn("Fill in the dates for me", html)
            self.assertRegex(html, r'class="nav" href="/teacher/prep" aria-current="page"')  # 1.30.0: the term plan is under Teach
            key = f"{school.class_id}:{school.syllabus_id}"
            status, html, _, _ = t.post("/teacher/plan", {"for": key, "action": "fill",
                                                          "term_start": (today - timedelta(days=30)).isoformat(),
                                                          "term_end": (today + timedelta(days=150)).isoformat()})
            self.assertIn("Spread every chapter over the term", html)
            with self.db.tenant(school.inst) as tx:
                ch = _chapters(tx, school.syllabus_id)
            c8 = ch["8"]
            ids = [c["id"] for c in ch.values()]
            form = {"for": key, "action": "save", "chapter[]": ids}
            for c in ch.values():
                form[f"start_{c['id']}"], form[f"end_{c['id']}"] = "", ""
            form[f"start_{c8['id']}"], form[f"end_{c8['id']}"] = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=6)).isoformat()
            status, html, _, _ = t.post("/teacher/plan", form)
            self.assertIn("Term plan saved: 1 chapter dated", html)
            self.assertIn("In class now", html)
            form[f"end_{c8['id']}"] = (today - timedelta(days=9)).isoformat()
            status, html, _, _ = t.post("/teacher/plan", form)
            self.assertEqual(status, 422)
            self.assertIn("Chapter 8: the end date is before the start date", html)
            html = t.get("/teacher")[1]
            self.assertIn(f"Ch 8 {c8['title']}", html)  # Today: the class's chapter in the term plan
            self.assertIn(f"/teacher/present/{c8['id']}?for={key}", html)
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = s.get("/student/learn")[1]
            self.assertIn("In class now", html)
            self.assertIn(f"Chapter 8: {c8['title']}", html)
            self.assertIn(f'href="/student/learn/{c8["topics"][0]["id"]}"', html)
            self.assertRegex(html, rf'id="ch{c8["id"]}" open')
        finally:
            live.stop()
