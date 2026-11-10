"""Simple flows (UX-1): one clear next action for students and teachers."""
from __future__ import annotations

import re

from novaquest.services import learn, mastery, path, pictures
from tests.base import PASSWORD, NQTestCase
from tests.live import Client, LiveServer


class LearningPath(NQTestCase):
    def test_topic_path_moves_step_by_step(self):
        """[UX-1][LN-10] Every topic is learnt in the same order: Learn in pictures → Practise → Prove it → Teach NOVA.
        Each step is ticked from the student's own evidence, there is always exactly one next step, and topics without
        a NOVA mix-up have three steps."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            topic = tx.one("""SELECT t.id, t.code, t.title, t.parent_id AS chapter_id FROM syllabus_nodes t
                              WHERE t.id=%s""", [school.topics["8.5"]])
            p = path.topic_path(tx, st.user_id, topic)
            self.assertEqual([s["key"] for s in p["steps"]], ["pictures", "practice", "understand", "teach"])
            self.assertEqual(p["next"]["key"], "pictures")
            self.assertEqual([s["state"] for s in p["steps"]], ["next", "later", "later", "later"])
            pictures.finish(tx, st, topic["id"])
            self.assertEqual(path.topic_path(tx, st.user_id, topic)["next"]["key"], "practice")
            mastery.record(tx, st.user_id, topic["id"], "practice", 0.4)
            p = path.topic_path(tx, st.user_id, topic)
            self.assertEqual(p["next"]["key"], "practice")
            self.assertTrue(p["next"]["tried"], "a low score asks for another try")
            mastery.record(tx, st.user_id, topic["id"], "practice", 0.8)
            mastery.record(tx, st.user_id, topic["id"], "understanding", 0.7)
            p = path.topic_path(tx, st.user_id, topic)
            self.assertEqual(p["next"]["key"], "teach")
            self.assertEqual(p["steps"][1]["score"], 80)
            mastery.record(tx, st.user_id, topic["id"], "teach", 0.9)
            p = path.topic_path(tx, st.user_id, topic)
            self.assertTrue(p["complete"])
            self.assertIsNone(p["next"])
            # a topic NOVA has no mix-up for gets three steps
            self.assertEqual([s["key"] for s in path._steps(topic, {}, False, True)], ["pictures", "practice", "understand"])

    def test_continue_picks_up_where_you_left_off(self):
        """[UX-1] 'Continue' resumes the most recently touched unfinished topic, else starts the demo chapter."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            chapters = learn.topics_for_student(tx, st)
            path.annotate(tx, st.user_id, chapters)
            first = path.continue_topic(tx, st.user_id, chapters)
            self.assertFalse(first["resume"])
            self.assertEqual(first["chapter"]["no"], "8")
            self.assertEqual(first["step"]["key"], "pictures")
            pictures.finish(tx, st, school.topics["3.2"])
            chapters = learn.topics_for_student(tx, st)
            path.annotate(tx, st.user_id, chapters)
            again = path.continue_topic(tx, st.user_id, chapters)
            self.assertTrue(again["resume"])
            self.assertEqual(again["topic"]["code"], "3.2")
            self.assertEqual(again["step"]["key"], "practice")


class SimpleScreens(NQTestCase):
    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def test_student_screens_have_one_next_action(self):
        """[UX-1] Student Home shows one 'Up next' (nearest due task first, otherwise the next learning step) and at most
        three other items; a topic page shows its 4 steps with ONE Continue button; Ask NOVA is a floating button;
        the tabs are Home, Learn, Tasks, Me; practice shows one question at a time with a final review."""
        school = self.make_school()
        st = Client(self.live.url)
        st.login_student("SUN", "8B", "1", school.students[0]["pin"])
        html = st.get("/student")[1]
        self.assertEqual(html.count("upnext"), 1)
        self.assertIn("Start: Learn in pictures", html, "with nothing due, Up next is the next learning step")
        self.assertEqual(re.findall(r'<nav class="tabbar".*?</nav>', html, re.S)[0].count("<li>"), 4)
        for label in ("Home", "Learn", "Tasks", "Me"):
            self.assertIn(f"{label}</a></li>", html)
        aid = self.open_assignment(school)
        html = st.get("/student")[1]
        self.assertIn(f'href="/student/assignments/{aid}"', html.split("Also for you")[0], "a due task comes first")
        self.assertLessEqual(html.count('class="topic-row"'), 3)
        tid = school.topics["8.5"]
        html = st.get(f"/student/learn/{tid}")[1]
        self.assertEqual(len(re.findall(r'class="btn block big"', html)), 1)
        self.assertIn(f'href="/student/learn/{tid}/pictures">Start: Learn in pictures', html)
        self.assertEqual(html.count('class="step '), 4)
        self.assertIn('<details class="card textbook">', html)
        self.assertIn("data-ask-open", html)
        self.assertIn('data-topic="%d"' % tid, html)
        learn_html = st.get("/student/learn")[1]
        self.assertIn('href="/student/pictures', learn_html, "Learn in pictures keeps its own entry")
        self.assertIn("Continue where you left off", learn_html, "opening a topic counts as starting it")
        practice = st.get(f"/student/learn/{tid}/practice")[1]
        self.assertIn("data-steps", practice)
        self.assertIn("data-review", practice)
        self.assertNotIn("Mission stage", practice, "the progress bar replaces the repeated stage text")

    def test_teacher_menu_home_and_assignment_stages(self):
        """[UX-1] Teachers get a 4-item menu (Today, Teach, Classes, Assignments) with a notification bell; Home is
        a to-do list with one button per row; an assignment shows its stage, ONE main action and Questions/Results tabs;
        a draft's questions can be approved in one click; Classes has Students and Progress tabs."""
        school = self.make_school()
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        html = t.get("/teacher")[1]
        nav = re.findall(r'<nav class="side.*?</nav>', html, re.S)[0]
        self.assertEqual(re.findall(r'class="nav" href="([^"]+)"', nav), ["/teacher", "/teacher/prep", "/teacher/classes", "/teacher/assignments"])
        self.assertIn('href="/notifications" aria-label="Notifications', nav)
        self.assertIn("You're all caught up", html)
        with self.db.tenant(school.inst) as tx:
            from novaquest.services import questions
            aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                                topic_ids=[school.topics["8.5"]], kind="quiz", difficulty="easy", count=3, title="")
        html = t.get("/teacher")[1]
        rows = re.findall(r'<li class="todo-row (\w+)">(.*?)</li>', html, re.S)
        self.assertEqual(rows[0][0], "draft")
        self.assertEqual(rows[0][1].count('class="btn'), 1)
        html = t.get(f"/teacher/assignments/{aid}")[1]
        self.assertIn('aria-current="step"><span class="sd">1</span>Draft', html)
        self.assertIn("Approve all 3 questions", html)
        self.assertIn('<span class="off" title="Results appear once it opens">Results</span>', html)
        status, html, _, url = t.post(f"/teacher/assignments/{aid}/approve-all")
        self.assertEqual(status, 200)
        self.assertTrue(url.endswith("#sch-h") and "msg=Approved%203" in url, url)
        self.assertNotIn("Approve all", html)
        self.assertIn('href="#sch-h">Schedule it', html)
        status, _, _, url = t.get("/teacher/classes")
        self.assertTrue(url.endswith(f"/teacher/classes/{school.class_id}/students"), "one class: straight to it")
        html = t.get(f"/teacher/classes/{school.class_id}/mastery")[1]
        self.assertIn(f'href="/teacher/classes/{school.class_id}/students"', html)
        self.assertIn('aria-current="page">Progress</a>', html)
        html = t.get("/teacher/assignments/new")[1]
        self.assertEqual(html.count('class="choice kind"'), 7, "every type shown (1.30.0: no 'More types' fold)")
        self.assertIn("data-topic-search", html)
