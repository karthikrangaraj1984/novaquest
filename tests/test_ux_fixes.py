"""1.7.1: defects from the UX review: teacher screens on a phone, honest class and school numbers, Ask NOVA on the
topic page, a safe 'new academic year', a parent link that opens a page, and Create assignment starting from the
chapter in class now. (Offline question giveaways are tested in test_question_quality.)"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from novaquest.core.errors import ValidationError
from novaquest.services import attempts, dashboards, institutions, mastery, plans, syllabus
from tests.base import PASSWORD, NQTestCase


class TeacherOnAPhone(NQTestCase):
    def test_menu_is_a_bottom_bar_and_the_class_list_fits(self):
        """[UX-1][NF-A11Y] On a phone (390 px) a teacher gets a slim top bar (bell and account) and the four menu items
        in a bar at the bottom, all fully visible; Sign out is in the account menu. The class list shows each student
        as a card, with nothing wider than the screen. No accessibility problems on Home, the class list or the term plan."""
        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS, PHONE, SHOTS

        school = self.make_school(n_students=6)
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
                for name, url in (("home", "/teacher"), ("students", f"/teacher/classes/{school.class_id}/students"),
                                  ("plan", "/teacher/plan")):
                    await page.goto(live.url + url)
                    out["problems"][name] = await page.evaluate(A11Y_JS)
                    await page.screenshot(path=str(SHOTS / f"phone_teacher_{name}.png"), full_page=True)
                    if name == "home":
                        out["nav"] = await page.evaluate("""() => [...document.querySelectorAll('nav.side a.nav')].map(a => {
                            const r = a.getBoundingClientRect();
                            return {text: a.innerText.trim(), left: r.left, right: r.right, bottom: r.bottom, h: innerHeight, w: innerWidth}; })""")
                        out["side"] = await page.evaluate("getComputedStyle(document.querySelector('nav.side')).position")
                        await page.click(".mtop .mmenu summary")
                        out["signout"] = await page.locator(".mtop .mmenu-pop button:has-text('Sign out')").is_visible()
                    if name == "students":
                        out["row"] = await page.evaluate("getComputedStyle(document.querySelector('table.cards tbody tr')).display")
                await b.close()
                return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        self.assertEqual(out["problems"], {"home": [], "students": [], "plan": []})
        self.assertEqual(out["side"], "fixed")
        self.assertEqual([n["text"] for n in out["nav"]], ["Today", "Teach", "Classes", "Assignments"])
        for n in out["nav"]:
            self.assertGreaterEqual(n["left"], 0, n)
            self.assertLessEqual(n["right"], n["w"] + 0.5, n)
            self.assertGreater(n["bottom"], n["h"] - 80, "the menu sits at the bottom of the screen")
        self.assertTrue(out["signout"])
        self.assertEqual(out["row"], "grid")


class HonestNumbers(NQTestCase):
    def test_class_card_counts_the_whole_class(self):
        """[DB-2][UX-1] The class card measures the whole syllabus for the whole class: one student practising one
        topic is '<1%' of it and '1 of 6 students started', never 'Practised 100%'."""
        school = self.make_school(n_students=6)
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, school.student(0).user_id, school.topics["8.3"], "practice", 0.8)
            lv = dashboards.class_mastery(tx, school.class_id)["levels"]
        self.assertEqual((lv["started_students"], lv["students"]), (1, 6))
        self.assertLess(lv["practised_or_better"], 1)
        self.assertGreater(lv["practised_or_better"], 0)
        from tests.live import Client, LiveServer

        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get("/teacher")[1]
        finally:
            live.stop()
        self.assertIn("1 of 6 students started · &lt;1% of the syllabus practised or mastered", html)
        self.assertNotIn("Practised 100%", html)

    def test_principal_sees_figures_only_with_enough_data_and_on_time_only_for_closed_work(self):
        """[DB-3] The principal's figures need at least 5 students with scores in a class ('Not enough data yet'
        otherwise; same for chapters and the weekly trend). 'On time' counts only assignments whose deadline has
        passed, over every student they were set for, so an open assignment never shows as 0% on time."""
        school = self.make_school(n_students=6)
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3, hours=24)
        for i in range(3):
            st = school.student(i)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid))
        with self.db.tenant(school.inst) as tx:
            d = dashboards.leadership(tx, school.leader)
        c = d["classes"][0]
        self.assertIsNone(c["understanding"], "3 students is not enough to describe a class")
        self.assertEqual(c["measured"], 3)
        self.assertIsNone(d["kpis"]["understanding"])
        self.assertEqual(d["chapters"], [])
        self.assertIsNone(c["on_time"], "still open: nothing is late yet")
        self.assertIsNone(d["kpis"]["on_time"])
        from tests.live import Client, LiveServer

        live = LiveServer(self.settings, self.db)
        try:
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            html = p.get("/leader")[1]
            self.assertIn("Not enough data yet", html)
            self.assertIn("None closed yet", html)
            self.assertIn("Not enough data (3 students)", html)
            self.assertIn("Lessons presented", html)  # 1.30.0: the teacher table
            self.assertRegex(html, r'Mrs Rao</th>\s*<td data-label="Teaches">Science 8B')
            self.assertIn("Shown once 5+ students have played", html, "the AI skills tile says why it has no figure")
        finally:
            live.stop()
        for i in range(3, 5):
            st = school.student(i)
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid))
        self.clock.advance(days=2)
        with self.db.tenant(school.inst) as tx:
            d = dashboards.leadership(tx, school.leader)
        c = d["classes"][0]
        self.assertIsNotNone(c["understanding"])
        self.assertEqual((c["on_time"], c["closed"]), (83, 1), "5 of the 6 students it was set for")
        self.assertTrue(d["chapters"])


class SmallerFixes(NQTestCase):
    def test_topic_page_ask_is_in_the_list_and_parent_link_opens_a_page(self):
        """[LN-2][SX-6] On a topic page Ask NOVA is a row in the list (no floating button over the cards), and it
        still opens the Ask NOVA sheet. A parent's link opens a readable page with a Download as PDF button; it isn't
        cached or passed on to other sites, and an expired link says so."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            from novaquest.services import reports
            link = reports.create_share_link(tx, school.teacher, "http://x", school.students[0]["student_id"])
        path = link.replace("http://x", "")
        live = LiveServer(self.settings, self.db)
        try:
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = s.get(f"/student/learn/{school.topics['8.3']}")[1]
            self.assertNotIn('class="ask-fab"', html)
            self.assertRegex(html, r'class="topic-row pic-short ask-row" type="button" data-ask-open')
            self.assertIn('id="ask-sheet"', html)
            self.assertIn('class="ask-fab"', s.get("/student/learn")[1], "other Learn pages keep the button")
            anon = Client(live.url)
            status, html, headers, _ = anon.get(path)
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["content-type"])
            self.assertIn("Student 1's learning progress", html)
            self.assertIn(f'href="{path}.pdf"', html)
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("referrer-policy"), "same-origin", "the link is never sent on to another site")
            status, _, headers, _ = anon.get(path + ".pdf")
            self.assertEqual((status, headers["content-type"]), (200, "application/pdf"))
            self.clock.advance(days=15)
            status, html, _, _ = anon.get(path)
            self.assertEqual(status, 404)
            self.assertIn("This report link has expired", html)
        finally:
            live.stop()

    def test_new_academic_year_moves_up_and_never_sideways(self):
        """[ON-5] 'New academic year' suggests the next grade's class of the same section and asks for it; with no
        next-year class yet it says to add one first. Moving students into the same grade in the same year is
        refused (that's a section change, done one student at a time)."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get("/admin")[1]
            self.assertIn("First add next year's classes above (for example 9B)", html)
            self.assertIn('<option value="">Choose next year\'s class…</option>', html)
            with self.db.tenant(school.inst) as tx:
                same = institutions.create_class(tx, school.admin, grade="8", section="A", academic_year="2026-27")
                nxt = institutions.create_class(tx, school.admin, grade="9", section="B", academic_year="2027-28")
                with self.assertRaises(ValidationError):
                    institutions.rollover(tx, school.admin, from_class_id=school.class_id, to_class_id=same)
            html = a.get("/admin")[1]
            self.assertRegex(html, rf'<option value="{nxt}" selected>9B \(2027-28\)</option>')
            self.assertNotIn("First add next year", html)
        finally:
            live.stop()

    def test_create_assignment_starts_from_the_chapter_in_class_now(self):
        """[DB-7][AS-1] With a term plan, Create assignment opens with the chapter in class now ticked and labelled
        (and says so); a topic chosen elsewhere (e.g. from a review) still wins."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        today = plans.today()
        with self.db.tenant(school.inst) as tx:
            ch = {c["code"]: c for c in syllabus.get_tree(tx, school.syllabus_id)}
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id,
                       [(ch["11"]["id"], today - timedelta(days=1), today + timedelta(days=6))])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get("/teacher/assignments/new")[1]
            ticked = set(re.findall(r'name="topics\[\]" value="(\d+)" checked', html))
            self.assertEqual(ticked, {str(x["id"]) for x in ch["11"]["topics"]})
            self.assertIn("Started with the chapter in class now", html)
            self.assertIn("In class now</span>", html)
            html = t.get(f"/teacher/assignments/new?topic={school.topics['8.3']}")[1]
            self.assertEqual(set(re.findall(r'name="topics\[\]" value="(\d+)" checked', html)), {str(school.topics["8.3"])})
            self.assertNotIn("Started with the chapter in class now", html)
        finally:
            live.stop()
