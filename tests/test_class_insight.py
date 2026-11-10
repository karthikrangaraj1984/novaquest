"""Seeing each student clearly (teacher review, items 8 and 9): one page per student, and a student × topic grid on
Progress from which a teacher sets revision for just the students who need it."""
from __future__ import annotations

import re
import time

from novaquest.core.errors import Forbidden
from novaquest.services import attempts, dashboards, learn, mastery, questions
from tests.base import PASSWORD, NQTestCase


class StudentPage(NQTestCase):
    def test_one_page_shows_everything_about_a_student(self):
        """[DB-1][LN-5] A student's page shows their level on every topic they've worked on (with the last activity and
        score), the assignments they handed in or missed, what they mixed up, their questions to NOVA, and when they
        last signed in. Only their teachers (and school leaders) can open it."""
        school = self.make_school()
        st, other = school.student(0), school.student(1)
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3, hours=2)
        att = self.answer_all(school, st, aid, good=False)
        attempts.submit(self.db, self.engine, st, att)
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, st.user_id, school.topics["8.3"], "practice", 0.7)
            tx.exec("UPDATE responses SET misconception_codes='{M8-1}' WHERE attempt_id=%s", [att])
        learn.ask(self.db, self.engine, st, "what does the nucleus do?")
        self.clock.advance(hours=3)
        with self.db.tenant(school.inst) as tx:
            p = dashboards.student_profile(tx, school.teacher, st.user_id)
            q = dashboards.student_profile(tx, school.teacher, other.user_id)
            dashboards.student_profile(tx, school.leader, st.user_id)
        self.assertEqual(p["student"]["name"], "Student 1")
        topics = {t["code"]: t for b in p["books"] for c in b["chapters"] for t in c["topics"]}
        self.assertEqual(topics["8.3"]["level"], 2)
        self.assertEqual(topics["8.3"]["last_score"], 70)
        self.assertIn("8.5", topics)
        self.assertEqual(p["handed"], 1)
        self.assertEqual(p["work"][0]["pct"], 0)
        self.assertEqual(p["mixups"][0]["label"], "All cells look the same")
        self.assertIn("nucleus", p["asked"][0]["body"])
        self.assertEqual(len(q["missed"]), 1, "the other student missed the closed quiz")
        b = self.make_school("MOON")
        with self.db.tenant(b.inst) as tx:
            with self.assertRaises(Exception):
                dashboards.student_profile(tx, b.teacher, st.user_id)
        with self.db.tenant(school.inst) as tx:
            stranger = self.staff(school.inst, "Mr Other", "other@sun.test", ["teacher"])
            with self.assertRaises(Forbidden):
                dashboards.student_profile(tx, stranger, st.user_id)


class ClassGrid(NQTestCase):
    def test_grid_shows_levels_and_picks_revision_topics(self):
        """[DB-2][LN-5] The grid shows one chapter (by default the one with the latest activity) with each student's
        level per topic and how many topics they're behind on; for the ticked students, revision targets the topics
        most of them haven't practised."""
        school = self.make_school(n_students=4)
        s1, s2 = school.student(0).user_id, school.student(1).user_id
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, s1, school.topics["8.3"], "practice", 0.9)
            mastery.record(tx, s2, school.topics["8.5"], "understanding", 0.9)
            g = dashboards.class_grid(tx, school.teacher, school.class_id)
            self.assertEqual(g["chapter"]["code"], "8", "the chapter with the latest activity")
            self.assertEqual(g["cells"][(s1, school.topics["8.3"])], 2)
            self.assertEqual(g["cells"][(s2, school.topics["8.5"])], 3)
            n = len(g["topics"])
            behind = {s["id"]: s["behind"] for s in g["students"]}
            self.assertEqual(behind[s1], n - 1)
            self.assertEqual(behind[school.student(2).user_id], n)
            other = dashboards.class_grid(tx, school.teacher, school.class_id, chapter_id=g["chapters"][1]["id"])
            self.assertEqual(other["chapter"]["code"], "2")
            picked = dashboards.revision_topics(tx, school.teacher, school.class_id, g["chapter"]["id"], [s1, s2])
            self.assertNotIn(school.topics["8.3"], picked[:1])
            self.assertTrue(set(picked) <= {t["id"] for t in g["topics"]})

    def test_grid_is_fast_for_a_big_class(self):
        """[NF-PERF] The grid for a class of 40 with activity on every topic of a chapter builds in under 300 ms."""
        school = self.make_school(n_students=40, consent=False)
        with self.db.tenant(school.inst) as tx:
            g = dashboards.class_grid(tx, school.teacher, school.class_id)
            for s in g["students"]:
                for t in g["topics"]:
                    mastery.record(tx, s["id"], t["id"], "check", 1.0)
        with self.db.tenant(school.inst) as tx:
            start = time.perf_counter()
            g = dashboards.class_grid(tx, school.teacher, school.class_id)
            took = time.perf_counter() - start
        self.assertEqual(len(g["cells"]), 40 * len(g["topics"]))
        self.assertLess(took, 0.3)


class RevisionFlowOverHttp(NQTestCase):
    def test_tick_students_make_revision_and_schedule_for_just_them(self):
        """[DB-2][SC-1] Over HTTP: Progress shows the grid; the teacher ticks two students and makes a revision task;
        Create assignment opens with the topics they most need ticked and says it's for those students; the draft's
        schedule form has just them chosen, and scheduling sends it only to them. The class list links to each
        student's page, which has a print button."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=5)
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, school.student(4).user_id, school.topics["8.5"], "check", 1.0)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/classes/{school.class_id}/mastery")[1]
            self.assertIn("Who needs help with what", html)
            self.assertEqual(html.count('class="cell c'), 5 * len(re.findall(r'class="gt"', html)) + 4)
            ch = re.search(r'name="chapter" value="(\d+)"', html).group(1)
            picked = [school.student(0).user_id, school.student(2).user_id]
            status, html, _, url = t.post(f"/teacher/classes/{school.class_id}/revise",
                                          {"students[]": picked, "chapter": ch, "book": school.syllabus_id})
            self.assertIn("Revision for 2 chosen students", html)
            self.assertIn("topics=", url)
            ticked = re.findall(r'name="topics\[\]" value="(\d+)" checked', html)
            self.assertTrue(ticked)
            status, html, _, url = t.post("/teacher/assignments/new", {
                "for": f"{school.class_id}:{school.syllabus_id}", "topics[]": ticked, "kind": "quiz", "difficulty": "easy",
                "count": "3", "title": "Revision", "for_students": ",".join(map(str, picked))})
            self.assertIn("set for the students you chose", html)
            aid = int(re.search(r"/teacher/assignments/(\d+)", url).group(1))
            for sid in picked:
                self.assertRegex(html, rf'name="students\[\]" value="{sid}" checked')
            self.assertNotRegex(html, rf'name="students\[\]" value="{school.student(1).user_id}" checked')
            with self.db.tenant(school.inst) as tx:
                for q in questions.assignment_questions(tx, aid):
                    questions.approve(tx, school.teacher, q["id"])
            t.post(f"/teacher/assignments/{aid}/schedule", {"who": "students", "students[]": picked,
                                                            "opens_at": "2026-10-15T10:05", "expires_at": "2026-10-17T18:00"})
            with self.db.tenant(school.inst) as tx:
                targets = {r["target_id"] for r in tx.all("SELECT target_id FROM assignment_targets WHERE assignment_id=%s", [aid])}
            self.assertEqual(targets, set(picked))
            html = t.get(f"/teacher/classes/{school.class_id}/students")[1]
            self.assertIn(f'href="/teacher/students/{school.student(0).user_id}"', html)
            status, html, _, _ = t.get(f"/teacher/students/{school.student(4).user_id}")
            self.assertEqual(status, 200)
            self.assertIn("data-print", html)
            self.assertIn("Mastered", html)
        finally:
            live.stop()
