"""1.15.0: a student with work from several subjects sees it all on Home and Tasks: every subject together by deadline,
each assignment tagged with its subject, work they've started (with how far they've got) apart from work not started,
and Tasks filtered by subject."""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from novaquest.core import clock
from novaquest.services import assignments, attempts, books, institutions, questions, syllabus
from tests.base import NQTestCase
from tests.test_books import made_up_book


class WorkAcrossSubjects(NQTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-work-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _maths(self, school) -> int:
        from novaquest.ai.offline import OfflineEngine

        store = self.tmp / "books"
        pdf = made_up_book(self.tmp / "maths.pdf")
        with pdf.open("rb") as fh:
            sid = books.create(self.db, school.admin, board="CBSE", subject="Maths", grade="8", title="Maths for You",
                               files=[(pdf.name, fh)], storage=store)
        books.read_book(self.db, OfflineEngine(), school.inst, sid, store)
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [school.syllabus_id, sid])
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id,
                                        syllabus_id=sid)
        return sid

    def _open(self, school, topic_ids, title, hours):
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id, topic_ids=topic_ids,
                                            kind="quiz", difficulty="medium", count=4, title=title)
        with self.db.tenant(school.inst) as tx:
            for q in questions.assignment_questions(tx, aid):
                questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                 expires_at=clock.now() + timedelta(hours=hours), targets=[("class", school.class_id)])
        return aid

    def test_home_and_tasks_show_every_subject_with_progress(self):
        """[UX-1][SX-1] Home keeps ONE 'Up next' (the soonest deadline, any subject) with its subject tag, then a line per
        subject ('2 to do · 1 started') that opens Tasks for that subject, then 'In progress' (with 'answered of
        questions') and 'To do', nearest deadline first, at most four, then 'See all N tasks'. Tasks lists In progress,
        Not started, Handed in, Results ready… with subject tags, and filters by subject."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        maths = self._maths(school)
        with self.db.tenant(school.inst) as tx:
            m_topics = [t["id"] for c in syllabus.get_tree(tx, maths) for t in c["topics"]]
        s = [school.topics[c] for c in ("8.3", "8.5", "8.2", "8.4")]
        ids = {"sci_soon": self._open(school, [s[0]], "Cell Lab", 5), "maths_mid": self._open(school, [m_topics[0]], "Motion quiz", 30),
               "sci_started": self._open(school, [s[1]], "Cells mission", 50), "maths_late": self._open(school, [m_topics[3]], "Levers quiz", 70),
               "sci_late": self._open(school, [s[2]], "Microbes quiz", 90), "sci_last": self._open(school, [s[3]], "Membrane quiz", 120)}
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, st, ids["sci_started"])
            qs = [q for q in questions.assignment_questions(tx, ids["sci_started"])]
            for q in qs[:2]:
                attempts.save_answer(tx, st, att["id"], q["id"], {"index": 0})
            items = {a["id"]: a for a in assignments.for_student(tx, st.user_id)}
        started = items[ids["sci_started"]]
        self.assertEqual((started["started"], started["answered"], started["questions"]), (True, 2, len(qs)))
        self.assertEqual(items[ids["maths_mid"]]["subject_label"], "Maths")
        self.assertTrue(items[ids["sci_soon"]]["closes_soon"])
        self.assertFalse(items[ids["maths_late"]]["closes_soon"])
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = c.get("/student")[1]
            due = html.split('id="due-h"')[1].split("</section>")[0]  # 1.31.0: Home lists work due in the next two days
            self.assertEqual(re.findall(r"<b>([^<]+)</b>", due), ["Cell Lab", "Motion quiz"], "soonest deadline first")
            self.assertIn("subj subj-", due, "each with its subject tag")
            self.assertIn("Closes today", due)
            self.assertIn("4 more open tasks", due)
            html = c.get("/student/tasks")[1]
            for group in ("In progress", "Not started"):
                self.assertIn(f">{group}</h2>", html)
            self.assertEqual(html.count('class="task-row"'), 6)
            html = c.get(f"/student/tasks?book={maths}")[1]
            self.assertEqual(html.count('class="task-row"'), 2)
            self.assertIn('aria-current="page"', html)
            self.assertNotIn("Cell Lab", html)
        finally:
            live.stop()

    def test_work_lists_fit_a_phone(self):
        """[UX-1][NF-A11Y] On a phone (390 px) Home and Tasks with several subjects fit the screen without sideways
        scrolling and pass the accessibility checks (labels, contrast, 44 px targets)."""
        import asyncio

        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS

        school = self.make_school(n_students=1)
        maths = self._maths(school)
        with self.db.tenant(school.inst) as tx:
            m_topics = [t["id"] for c in syllabus.get_tree(tx, maths) for t in c["topics"]]
        self._open(school, [school.topics["8.3"]], "Cell Lab mission with a long name for small screens", 5)
        self._open(school, [m_topics[0]], "Motion quiz", 30)
        self._open(school, [school.topics["8.5"]], "Cells quiz", 50)
        live = LiveServer(self.settings, self.db)

        async def go():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                page = await (await b.new_context(viewport={"width": 390, "height": 844})).new_page()
                await page.goto(live.url + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", school.students[0]["pin"])
                await page.click("button[type=submit]")
                out = {}
                for path in ("/student", "/student/tasks", f"/student/tasks?book={maths}"):
                    await page.goto(live.url + path)
                    out[path] = {"wide": await page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
                                 "a11y": await page.evaluate(A11Y_JS)}
                await b.close()
                return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        for path, r in out.items():
            self.assertFalse(r["wide"], f"{path}: no sideways scrolling")
            self.assertEqual(r["a11y"], [], path)
