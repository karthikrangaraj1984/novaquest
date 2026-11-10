"""Fixes from a Grade 8 student's review of the app: honest results with a clear next step, Ask NOVA out of the way,
help to start writing, a Me page that shows what's done, a clearer Up next, easier sign-in, and celebrations."""
from __future__ import annotations

from novaquest.services import assignments, attempts, auth, dashboards, mastery, path, review, syllabus, teach
from novaquest.web.routes.learner import _topic_cheer
from tests.base import PASSWORD, NQTestCase


class HonestResults(NQTestCase):
    def test_low_score_is_never_praised_and_says_what_to_revise(self):
        """[SX-1][LN-9] A student who got everything wrong sees an honest, kind headline (never 'Nice work' or
        'Excellent'), with each concept's percentage and a link to revise the weak topics. A strong result is praised."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        bad, good = school.student(0), school.student(1)
        attempts.submit(self.db, self.engine, bad, self.answer_all(school, bad, aid, good=False))
        attempts.submit(self.db, self.engine, good, self.answer_all(school, good, aid, good=True))
        with self.db.tenant(school.inst) as tx:
            review.release(tx, school.teacher, aid)
            low = review.scorecard(tx, bad, aid)
            high = review.scorecard(tx, good, aid)
        self.assertTrue(low["headline"][0].startswith("This one was tricky: 0 of"), low["headline"])
        self.assertNotRegex(" ".join(low["headline"]), r"Nice|Excellent|Good work")
        self.assertIn(school.topics["8.5"], {r["topic_id"] for r in low["revise"]})
        self.assertTrue(all("pct" in c for c in low["concepts"]))
        self.assertTrue(high["headline"][0].startswith("Excellent!"), high["headline"])
        self.assertEqual(high["revise"], [])


class MePageAndStreak(NQTestCase):
    def test_me_page_counts_what_is_done_and_splits_levels(self):
        """[TA-7][SX-5] The Me page counts picture lessons, practised topics, understanding checks and topics taught
        to NOVA, and each chapter shows how many topics are mastered, practised and still being learnt."""
        school = self.make_school()
        st = school.student(0)
        t = school.topics
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, st.user_id, t["8.3"], "pictures", None)
            mastery.record(tx, st.user_id, t["8.3"], "practice", 0.7)
            mastery.record(tx, st.user_id, t["8.5"], "understanding", 0.9)
            mastery.record(tx, st.user_id, t["8.6"], "practice", 0.2)
            ov = dashboards.student_overview(tx, st)
        self.assertEqual(ov["done"], {"practised": 2, "pictures": 1, "checked": 1, "taught": 0})
        ch = next(c for c in ov["chapters"] if c["no"] == "8")
        self.assertEqual((ch["mastered"], ch["practised"], ch["learning"]), (1, 1, 1))
        self.assertEqual(ov["streak"], 1)

    def test_weekly_streak_counts_weeks_in_a_row_and_resets_after_a_gap(self):
        """[TA-7] The streak counts weeks in a row with some learning (a weekend off never breaks it); it carries over
        until the end of the next week, and a gap of a whole week starts it again."""
        school = self.make_school()
        sid, tid = school.student(0).user_id, school.topics["8.3"]
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(dashboards.week_streak(tx, sid), 0)
            mastery.record(tx, sid, tid, "practice", 0.5)
        for _ in range(2):
            self.clock.advance(days=7)
            with self.db.tenant(school.inst) as tx:
                mastery.record(tx, sid, tid, "practice", 0.5)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(dashboards.week_streak(tx, sid), 3)
        self.clock.advance(days=7)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(dashboards.week_streak(tx, sid), 3, "nothing yet this week: last week still counts")
        self.clock.advance(days=14)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(dashboards.week_streak(tx, sid), 0)
            mastery.record(tx, sid, tid, "practice", 0.5)
            self.assertEqual(dashboards.week_streak(tx, sid), 1)


class TopicCelebration(NQTestCase):
    def test_finishing_every_step_of_a_topic_is_celebrated(self):
        """[LN-3] When every step of a topic is done the student sees 'Topic complete!'; not before."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            topic = syllabus.get_topic(tx, school.topics["8.5"])
            mastery.record(tx, st.user_id, topic["id"], "practice", 1.0)
            steps = path.topic_path(tx, st.user_id, topic)
            self.assertIsNone(_topic_cheer(tx, st, topic, steps))
            for kind in ("pictures", "understanding", "teach"):
                mastery.record(tx, st.user_id, topic["id"], kind, None if kind == "pictures" else 1.0)
            steps = path.topic_path(tx, st.user_id, topic)
            self.assertTrue(steps["complete"], steps)
            cheer = _topic_cheer(tx, st, topic, steps)
        self.assertEqual(cheer[0], "Topic complete! 🎉")
        self.assertIn(topic["title"], cheer[1])


class SmallHelpers(NQTestCase):
    def test_class_typed_any_way_and_task_summary(self):
        """[ST-2][UX-1] '8 b', '8-B' and '8B' are the same class at sign-in. Up next says how many questions and about
        how long; Teach NOVA's sentence starters are short enough to read in full."""
        for typed in ("8 b", "8-B", " 8b ", "8B"):
            self.assertEqual(auth.class_key(typed), "8B")
        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        with self.db.tenant(school.inst) as tx:
            a = next(x for x in assignments.for_student(tx, school.student(0).user_id) if x["id"] == aid)
            info = assignments.task_summary(tx, a)
            self.assertEqual(info["questions"], 3)
            self.assertEqual(info["minutes"], 3)
            self.assertFalse(info["timed"])
            self.assertTrue(info["about"])
            ideas = {"key_ideas": [["cells of the same kind group together to form tissues in plants and animals"]]}
            first = teach.starters(ideas, "Tissues")[0]
            self.assertLessEqual(len(first.split()), 9, first)


class StudentJourneyOverHttp(NQTestCase):
    def test_student_screens_after_the_review(self):
        """[UX-1][SX-1][TA-7] Over HTTP: a student signs in typing '8 b'; the sign-in page says what to do about a
        forgotten PIN; Up next shows the question count and time; handing in is celebrated; the scorecard is honest
        with Revise links; the Me page shows what's done; practice and pictures have a small Ask button instead of
        the big one over the cards; the understanding check offers sentence starters and a link back to the topic, without Ask NOVA."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        live = LiveServer(self.settings, self.db)
        try:
            s = Client(live.url)
            html = s.get("/login/student")[1]
            self.assertIn("Forgot your PIN? Ask your teacher", html)
            status, html, _, url = s.login_student("SUN", "8 b", "1", school.students[0]["pin"])
            self.assertIn("/student", url)
            html = s.get("/student")[1]
            self.assertRegex(html, r"3 questions · about 3 min")
            with self.db.tenant(school.inst) as tx:
                att = attempts.start_or_resume(tx, school.student(0), aid)["id"]
                for q in tx.all("""SELECT q.id, q.answer, q.options FROM questions q JOIN assignment_questions aq
                                   ON aq.question_id=q.id WHERE aq.assignment_id=%s""", [aid]):
                    attempts.save_answer(tx, school.student(0), att, q["id"], {"index": (q["answer"]["index"] + 1) % len(q["options"])})
            html = s.post(f"/student/attempts/{att}/submit")[1]
            self.assertIn("Handed in! 🎉", html)
            with self.db.tenant(school.inst) as tx:
                review.release(tx, school.teacher, aid)
            html = s.get(f"/student/results/{aid}")[1]
            self.assertIn("This one was tricky", html)
            self.assertNotIn("Nice work", html)
            self.assertIn(f'href="/student/learn/{school.topics["8.5"]}"', html)
            html = s.get("/student/progress")[1]
            self.assertIn("My scores", html)  # 1.31.0: My progress
            self.assertIn("1 week in a row", html)
            tid = school.topics["8.3"]
            html = s.get(f"/student/learn/{tid}/practice")[1]
            self.assertIn('class="ask-mini"', html)
            self.assertIn("your teacher doesn't see these marks", html)
            html = s.get(f"/student/learn/{tid}/understand")[1]
            self.assertIn("2–3 sentences", html)
            self.assertIn(f'href="/student/learn/{tid}"', html)
            self.assertIn("It happens because", html)
            self.assertNotIn('class="ask-fab"', html, "the check is in the student's own words")
        finally:
            live.stop()
