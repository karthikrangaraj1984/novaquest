"""The principal's view (principal's review, 1.9.0): every link works, 'needs your attention', honest AI skills, a
read-only class page, a friendly reminder to a teacher, the weekly summary and the monthly report."""
from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from novaquest.core.errors import Forbidden
from novaquest.services import attempts, escape, institutions, notify, plans, principal, review, students
from tests.base import PASSWORD, NQTestCase
from tests.test_escape import right_answer


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def realistic_school(test: NQTestCase):
    """Three sections: 8B (Mrs Rao, 30 students) active with a closed quiz, a term plan and escape rooms; 8A and 8C
    (Mr Iyer) with students but no activity. Two students in 8B miss three closed quizzes."""
    school = test.make_school(n_students=30)
    iyer = test.staff(school.inst, "Mr Iyer", "iyer@sun.test", ["teacher"])
    with test.db.tenant(school.inst) as tx:
        others = []
        for sec in ("A", "C"):
            cid = institutions.create_class(tx, school.admin, grade="8", section=sec, academic_year="2026-27")
            institutions.assign_teacher(tx, school.admin, teacher_id=iyer.user_id, class_id=cid, syllabus_id=school.syllabus_id)
            added = students.import_rows(tx, iyer, cid, [{"roll_no": str(i + 1), "name": f"Pupil {sec}{i + 1}",
                                                          "parent_email": f"p{sec}{i}@sun.test"} for i in range(8)], skip_invalid=False).added
            for s in added:
                students.record_consent(tx, iyer, s["enrolment_id"], parent_name="Parent", method="signed_form")
            others.append(cid)
        plans.autofill(tx, school.teacher, school.class_id, school.syllabus_id, plans.today() - timedelta(days=100),
                       plans.today() + timedelta(days=120))
    quizzes = []
    for _ in range(3):
        aid = test.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=3, hours=2)
        for i in range(28):  # students 29 and 30 never hand in
            st = school.student(i)
            attempts.submit(test.db, test.engine, st, test.answer_all(school, st, aid, good=i % 3 != 0))
        quizzes.append(aid)
    with test.db.tenant(school.inst) as tx:
        for i in range(6):
            for n, lk in enumerate(escape.get_room(8)["locks"], 1):
                escape.attempt(tx, school.student(i), 8, n, right_answer(lk))
    test.clock.advance(hours=3)
    with test.db.tenant(school.inst) as tx:
        for aid in quizzes:
            review.release(tx, school.teacher, aid)
    return school, iyer, others, quizzes


class EveryLinkWorks(NQTestCase):
    def test_principal_can_follow_every_link_without_an_error(self):
        """[DB-9][UX-1] Starting from the Overview, a principal follows every link on every page they can reach (class
        pages, progress grids, escape rooms and guides, assignments and their results, students, tutor logs, reports,
        the syllabus): none leads to an error or an access-denied page."""
        from tests.live import Client, LiveServer

        school, iyer, others, quizzes = realistic_school(self)
        live = LiveServer(self.settings, self.db)
        bad, seen = [], set()
        try:
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            todo = ["/leader"]
            while todo and len(seen) < 260:
                path = todo.pop(0)
                if path in seen:
                    continue
                seen.add(path)
                status, html, headers, url = p.get(path)
                if status >= 400:
                    bad.append((path, status))
                    continue
                if "text/html" not in headers.get("content-type", ""):
                    continue
                parser = _Links()
                parser.feed(html)
                for href in parser.links:
                    u = urlparse(urljoin(live.url + path, href))
                    if u.netloc != urlparse(live.url).netloc or u.path in ("/logout",) or u.path.endswith((".jpg",)):
                        continue
                    nxt = u.path + (f"?{u.query}" if u.query else "")
                    if nxt not in seen and not re.search(r"/(present|worksheet)$", u.path) or u.path.startswith("/leader"):
                        todo.append(nxt)
        finally:
            live.stop()
        self.assertEqual(bad, [])
        self.assertGreater(len(seen), 60, "the crawl reached the class, student and assignment pages")
        self.assertTrue(any(s.startswith("/leader/classes/") for s in seen))
        self.assertTrue(any(s.startswith("/teacher/students/") for s in seen))
        self.assertTrue(any("/review" in s for s in seen))


class Overview(NQTestCase):
    def test_needs_attention_teachers_and_missed_work(self):
        """[DB-3][DB-9] 'Needs your attention' names the classes with no activity (and their teacher), a teacher who
        hasn't set any work, and the students who've missed three or more assignments (only those, not everyone
        who missed one). Each class row shows its term-plan status, escape rooms and last activity. A principal
        can send a teacher a friendly reminder (one a day, refreshed if sent again); teachers can't."""
        school, iyer, others, quizzes = realistic_school(self)
        with self.db.tenant(school.inst) as tx:
            d = principal.overview(tx, school.leader)
            texts = [x["text"] for x in d["concerns"]]
            self.assertIn("8A has had no NovaQuest activity yet (Mr Iyer).", texts)
            self.assertIn("8C has had no NovaQuest activity yet (Mr Iyer).", texts)
            self.assertIn("Mr Iyer (8A, 8C) hasn't set any assignments yet.", texts)
            self.assertIn("2 students have missed 3 or more assignments.", texts)
            self.assertNotIn("Mrs Rao", " ".join(texts))
            self.assertEqual(sorted(s["name"] for s in d["missed"]), ["Student 29", "Student 30"])
            b = next(c for c in d["classes"] if c["label"] == "8B")
            self.assertTrue(b["plan"]["planned"])
            self.assertEqual(b["rooms"], {"players": 6, "escaped": 0})
            self.assertFalse(b["idle"])
            name = principal.nudge_teacher(tx, school.leader, iyer.user_id)
            principal.nudge_teacher(tx, school.leader, iyer.user_id, "Please start with chapter 8 this week.")
            self.assertEqual(name, "Mr Iyer")
            self.assertEqual(notify.unread_count(tx, iyer.user_id), 1)
            self.assertIn("chapter 8", tx.val("SELECT body FROM notifications WHERE user_id=%s AND kind='nudge'", [iyer.user_id]))
            with self.assertRaises(Forbidden):
                principal.nudge_teacher(tx, school.teacher, iyer.user_id)

    def test_ai_skills_need_five_students(self):
        """[DB-8][DB-3] The principal's AI skills appear only for skills with at least 5 students behind them; with
        fewer, the tile says 'Not enough data yet' instead of a percentage."""
        school = self.make_school(n_students=8)
        with self.db.tenant(school.inst) as tx:
            for i in range(4):
                lk = escape.get_room(8)["locks"][0]
                escape.attempt(tx, school.student(i), 8, 1, right_answer(lk))
            d = principal.overview(tx, school.leader)
            self.assertEqual(d["ai_skills"], [])
            self.assertIsNone(d["kpis"]["ai"])
            escape.attempt(tx, school.student(4), 8, 1, right_answer(escape.get_room(8)["locks"][0]))
            d = principal.overview(tx, school.leader)
            self.assertEqual([s["concept"] for s in d["ai_skills"]], ["Sorting by clues"])
            self.assertEqual(d["kpis"]["ai"], 100)

    def test_overview_and_class_page_over_http(self):
        """[DB-9][SX-4] Over HTTP: the Overview links every class to its page and explains its numbers; a class page
        shows the term plan, weakest topics, work set with how many handed in, AI skills (or 'not tried') and the
        students, highlighting those who missed three or more. A reminder sent from the Overview reaches the
        teacher's bell."""
        from tests.live import Client, LiveServer

        school, iyer, others, quizzes = realistic_school(self)
        live = LiveServer(self.settings, self.db)
        try:
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            html = p.get("/leader")[1]
            self.assertIn("Needs your attention (", html)
            self.assertIn(f'href="/leader/classes/{school.class_id}"', html)
            self.assertIn("What do these numbers mean?", html)
            self.assertIn("Send a friendly reminder", html)
            html = p.get(f"/leader/classes/{school.class_id}")[1]
            for part in ("Term plan", "Weakest topics", "Work set", "AI skills", "Students"):
                self.assertIn(part, html)
            self.assertIn("28 of 30 handed in", html)
            self.assertIn('<b class="warn-text">3</b>', html)
            self.assertIn(f'href="/teacher/assignments/{quizzes[0]}/review"', html)
            html = p.get(f"/leader/classes/{others[0]}")[1]
            self.assertIn("No term plan yet", html)
            self.assertIn("No work set yet", html)
            status, html, _, _ = p.post("/leader/nudge", {"teacher_id": str(iyer.user_id)})
            self.assertIn("Reminder sent to Mr Iyer", html)
            t = Client(live.url)
            t.login("iyer@sun.test", PASSWORD)
            self.assertIn("A note from Dr Principal", t.get("/notifications")[1])
            self.assertEqual(t.post("/leader/nudge", {"teacher_id": str(iyer.user_id)})[0], 403)
            html = p.get("/teacher/syllabus")[1]
            self.assertIn("A read-only view", html)
            self.assertNotIn("Lock a topic", html)
        finally:
            live.stop()


class SummaryAndReport(NQTestCase):
    def test_weekly_summary_goes_out_once_each_monday_morning(self):
        """[DB-10] Each Monday from 7 am (Indian time) every principal gets the week's summary once, in the bell and by
        email: the active share and the 'needs your attention' list. Before 7 am on Monday nothing goes; the next
        week a new one does."""
        from novaquest import worker

        school, iyer, others, quizzes = realistic_school(self)
        self.clock.set(datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc))  # Monday 5:30 am IST
        self.assertEqual(worker.weekly_summaries(self.db), 0)
        self.clock.set(datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc))  # Monday 7:30 am IST
        self.assertEqual(worker.weekly_summaries(self.db), 1)
        self.assertEqual(worker.weekly_summaries(self.db), 0, "once a week")
        with self.db.tenant(school.inst) as tx:
            n = tx.one("SELECT title, body FROM notifications WHERE user_id=%s AND kind='weekly'", [school.leader.user_id])
            self.assertTrue(n["title"].startswith("Weekly summary:"))
            self.assertIn("8A has had no NovaQuest activity", n["body"])
            self.assertEqual(tx.val("SELECT count(*) FROM email_outbox WHERE to_addr='principal@sun.test'"), 1)
        self.clock.advance(days=7)
        self.assertEqual(worker.weekly_summaries(self.db), 1)

    def test_monthly_report_pdf(self):
        """[DB-10][DB-4] The monthly report is a one-page PDF for a management meeting: students, activity, work set,
        escape rooms and on-time share; each class with its month's activity, understanding (or 'Not enough data'),
        work set, on-time share and term-plan status; teachers; AI skills; and what needs attention. A wrong month is
        refused with a plain message."""
        import pypdf

        from tests.live import Client, LiveServer

        school, iyer, others, quizzes = realistic_school(self)
        live = LiveServer(self.settings, self.db)
        try:
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            status, body, headers, _ = p.get("/leader/report.pdf?month=2026-10")
            self.assertEqual((status, headers["content-type"]), (200, "application/pdf"))
            status, html, _, _ = p.get("/leader/report.pdf?month=October")
            self.assertEqual(status, 422)
            self.assertIn("Choose a month like 2026-10", html)
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            data = principal.monthly(tx, school.leader, principal.parse_month("2026-10"))
        from novaquest.services import pdfs
        reader = pypdf.PdfReader(io.BytesIO(pdfs.monthly_report(data)))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        for part in ("NovaQuest report, October 2026", "Classes", "8A", "8B", "Not enough data", "Mr Iyer", "Needs attention",
                     "No plan", "AI skills"):
            self.assertIn(part, text)
        self.assertRegex(text, r"\d+ behind|On track", "8B's plan status")
        self.assertEqual(data["assignments"], 3)


class PrincipalOnAPhone(NQTestCase):
    def test_overview_and_class_page_fit_a_phone(self):
        """[NF-A11Y][DB-9] On a phone the principal's Overview and class page have nothing wider than the screen (the
        class table becomes cards) and no accessibility problems."""
        import asyncio

        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS, PHONE, SHOTS

        school, iyer, others, quizzes = realistic_school(self)
        live = LiveServer(self.settings, self.db)
        SHOTS.mkdir(parents=True, exist_ok=True)

        async def go():
            async with async_playwright() as pw:
                b = await pw.chromium.launch()
                page = await (await b.new_context(**PHONE)).new_page()
                await page.goto(live.url + "/login")
                await page.fill("#email", "principal@sun.test")
                await page.fill("#password", PASSWORD)
                await page.click("button[type=submit]")
                await page.wait_for_load_state()
                out = {}
                for name, url in (("overview", "/leader"), ("class", f"/leader/classes/{school.class_id}")):
                    await page.goto(live.url + url)
                    out[name] = await page.evaluate(A11Y_JS)
                    await page.screenshot(path=str(SHOTS / f"principal_{name}.png"), full_page=True)
                await b.close()
                return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        self.assertEqual(out, {"overview": [], "class": []})
