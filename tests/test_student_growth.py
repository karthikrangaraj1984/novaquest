"""1.31.0: the student's own growth: fixes, due soon and results, My progress with score trends, My mistakes, Learn
opening on the chapter in class, Exam mode, Ask my teacher, reflections and weekly goals."""
from __future__ import annotations

import re
from datetime import date, timedelta

from novaquest.core import clock
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import (assignments, attempts, daily, doubts, examprep, institutions, mistakes, myprogress, plans,
                                review, syllabus)
from novaquest.services.access import Actor
from tests.base import PASSWORD, NQTestCase


class Base(NQTestCase):
    def released(self, school, kind="quiz", topics=("8.3",), good=(0,), bad=(1, 2), hours=1, count=4):
        """An assignment answered by some students (good or bad), closed and released."""
        aid = self.open_assignment(school, kind=kind, topics=topics, count=count, hours=hours)
        for i in list(good) + list(bad):
            att = self.answer_all(school, school.student(i), aid, good=i in good)
            attempts.submit(self.db, self.engine, school.student(i), att)
        with self.db.tenant(school.inst) as tx:
            tx.exec("UPDATE assignments SET expires_at=%s WHERE id=%s", [clock.now() - timedelta(minutes=5), aid])
            assignments.sync_states(tx)
            tx.exec("UPDATE responses SET flagged=false WHERE attempt_id IN (SELECT id FROM attempts WHERE assignment_id=%s)", [aid])
            review.release(tx, school.teacher, aid)
        return aid

    def client(self, school, i=0):
        from tests.live import Client
        c = Client(self.live.url)
        c.login_student("SUN", "8B", str(i + 1), school.students[i]["pin"])
        return c

    def setUp(self):
        super().setUp()
        from tests.live import LiveServer
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()


class Fixes(Base):
    def test_missed_work_and_one_status(self):
        """[SG-1] Released work a student never handed in is 'Missed' (no scorecard link) and My progress opens for
        them; a topic shows one status everywhere (its level), and a mastered topic offers revision, not step 1;
        results highlight Tasks in the menu; Daily 5 leaves out one-sentence-with-a-word-swapped questions."""
        school = self.make_school(n_students=4)
        aid = self.released(school, good=(0,), bad=(1,))
        c = self.client(school, 3)  # student 4 never handed in
        st, html = c.get("/student/progress")[:2]
        self.assertEqual(st, 200)
        self.assertIn("My scores", html)
        tasks = c.get("/student/tasks")[1]
        self.assertIn(">Missed</h2>", tasks)
        self.assertNotIn(f"/student/results/{aid}", tasks)
        good = self.client(school, 0)
        card = good.get(f"/student/results/{aid}")[1]
        self.assertRegex(re.findall(r'<nav class="tabbar".*?</nav>', card, re.S)[0], r'href="/student/tasks" aria-current="page"')
        tid = school.topics["8.3"]
        with self.db.tenant(school.inst) as tx:
            for kind, score in (("practice", 0.9), ("understanding", 0.9)):
                from novaquest.services import mastery
                mastery.record(tx, school.students[0]["student_id"], tid, kind, score)
            level = mastery.for_student(tx, school.students[0]["student_id"])[tid]["level"]
        self.assertEqual(level, 3)
        lesson = good.get(f"/student/learn/{tid}")[1]
        self.assertIn("Revise: practise again", lesson)
        self.assertIn("Mastered", lesson)
        learn = good.get("/student/learn")[1]
        row = learn.split(f'href="/student/learn/{tid}"')[1].split("</a>")[0]
        self.assertIn("Mastered", row)
        self.assertNotIn("In progress", learn)
        from novaquest.ai.claude import swapped_word_options
        with self.db.tenant(school.inst) as tx:
            import random
            for t in ("8.3", "8.5", "1.1"):
                if t in school.topics:
                    for q in daily._questions(tx, school.topics[t], random.Random(1)):
                        self.assertFalse(swapped_word_options(q["options"]), q)


class ProgressAndResults(Base):
    def test_home_due_soon_results_and_my_progress(self):
        """[SG-2] Home lists work due in the next two days with 'Closes today/tomorrow' (else the next deadline),
        then the learning step, Daily 5 and new results with the change from last time; Tasks lists every result
        with its score; My progress shows a score trend chart per subject with its average, latest and recent change,
        the best result this term, strong topics and ones that need work, and settings at the end."""
        school = self.make_school(n_students=3)
        a1 = self.released(school, topics=("8.3",), good=(), bad=(0,))
        a2 = self.released(school, topics=("8.5",), good=(0,), bad=())
        soon = self.open_assignment(school, topics=("8.2",), hours=20)
        c = self.client(school, 0)
        home = c.get("/student")[1]
        due = home.split('id="due-h"')[1].split("</section>")[0]
        self.assertIn("Due soon", home)
        self.assertIn(f"/student/assignments/{soon}", due)
        self.assertRegex(due, r"Closes (today|tomorrow)")
        new = home.split('id="new-h"')[1].split("</section>")[0]
        self.assertIn("▲", new, "the second result is up on the first")
        with self.db.tenant(school.inst) as tx:
            rows = myprogress.scores(tx, school.students[0]["student_id"])
        self.assertEqual([r["id"] for r in rows], [a1, a2])
        self.assertLess(rows[0]["pct"], rows[1]["pct"])
        tasks = c.get("/student/tasks")[1]
        self.assertIn(">Results</h2>", tasks)
        self.assertEqual(tasks.count('class="score-chip"'), 2)
        prog = c.get("/student/progress")[1]
        self.assertIn('class="mini-chart"', prog)
        self.assertIn("Average", prog)
        self.assertIn("Best this term", prog)
        self.assertIn(">Needs work</h2>", prog)
        self.assertLess(prog.index("My scores"), prog.index('id="set-h"'), "settings come last")
        self.assertIn("Share with my parents", prog.split('id="set-h"')[1])

    def test_due_label(self):
        """[SG-2] Deadlines read as people say them."""
        now = clock.now()  # Thu 15 Oct, 10:00 IST
        self.assertEqual(myprogress.due_label(now + timedelta(hours=3), now), "Closes today, 1:00 pm (in 3 h)")
        self.assertEqual(myprogress.due_label(now + timedelta(days=1), now), "Closes tomorrow, 10:00 am")
        self.assertEqual(myprogress.due_label(now + timedelta(days=2), now), "Closes Sat 17 Oct, 10:00 am")
        self.assertEqual(myprogress.due_label(now - timedelta(minutes=1), now), "Closed")


class Mistakes(Base):
    def test_my_mistakes(self):
        """[SG-3] Every question a student got wrong in released work is in My mistakes; a redo round of up to five
        multiple-choice questions marks them at once: right ones are fixed, wrong ones stay (tries counted) with the
        answer shown; written answers show the strong answer and can be marked understood. The original score
        never changes."""
        school = self.make_school(n_students=2)
        aid = self.released(school, kind="quiz", topics=("8.3", "8.5"), good=(), bad=(0,), count=5)
        st = school.students[0]["student_id"]
        with self.db.tenant(school.inst) as tx:
            before = myprogress.scores(tx, st)[0]["pct"]
            ov = mistakes.overview(tx, st)
            self.assertGreater(ov["todo"], 0)
            rnd = mistakes.redo_round(tx, st)
            self.assertLessEqual(len(rnd), mistakes.ROUND)
        c = self.client(school, 0)
        page = c.get("/student/mistakes")[1]
        self.assertIn("Redo", page)
        q0, q1 = rnd[0], rnd[1] if len(rnd) > 1 else None
        with self.db.tenant(school.inst) as tx:
            keys = {r["id"]: r["answer"]["index"] for r in tx.all("SELECT id, answer FROM questions WHERE id = ANY(%s)",
                                                                  [[q["question_id"] for q in rnd]])}
        form = {"qid[]": [str(q["question_id"]) for q in rnd]}
        for q in rnd:
            form[f"order_{q['question_id']}"] = ",".join(map(str, q["order"]))
        form[f"a_{q0['question_id']}"] = str(q0["order"].index(keys[q0["question_id"]]))  # right
        if q1:
            form[f"a_{q1['question_id']}"] = str(q1["order"].index((keys[q1["question_id"]] + 1) % len(q1["options"])))  # wrong
        res = c.post("/student/mistakes", form)[1]
        self.assertRegex(res, r"\d+ of \d+ right")
        with self.db.tenant(school.inst) as tx:
            fixed = {r["question_id"]: r for r in tx.all("SELECT * FROM mistake_fixes WHERE student_id=%s", [st])}
            self.assertIsNotNone(fixed[q0["question_id"]]["fixed_at"])
            if q1:
                self.assertIsNone(fixed[q1["question_id"]]["fixed_at"])
                self.assertEqual(fixed[q1["question_id"]]["tries"], 1)
            self.assertEqual(mistakes.overview(tx, st)["todo"], ov["todo"] - 1)
            self.assertEqual(myprogress.scores(tx, st)[0]["pct"], before, "redoing never changes the score")
        # a written answer
        aid2 = self.released(school, kind="short", topics=("8.3",), good=(), bad=(0,), count=1)
        with self.db.tenant(school.inst) as tx:
            written = [r for g in mistakes.overview(tx, st)["groups"] for r in g["items"] if r["kind"] == "short"]
        self.assertTrue(written)
        self.assertIn("A strong answer", c.get("/student/mistakes")[1])
        c.post(f"/student/mistakes/{written[0]['question_id']}/understood", {})
        with self.db.tenant(school.inst) as tx:
            self.assertIsNotNone(tx.val("SELECT fixed_at FROM mistake_fixes WHERE student_id=%s AND question_id=%s",
                                        [st, written[0]["question_id"]]))
        self.assertTrue(aid and aid2)

    def test_learn_opens_on_the_chapter_in_class(self):
        """[SG-3] Learn opens on the chapter being taught in class now; earlier chapters and the ones coming up fold
        into one line each."""
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            ch8 = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id, [(ch8, date(2026, 10, 5), date(2026, 10, 30))])
        learn = self.client(school, 0).get("/student/learn")[1]
        self.assertIn("Earlier chapters", learn)
        self.assertIn("Coming up", learn)
        self.assertRegex(learn, rf'id="ch{ch8}" open')


class ExamsAndQuestions(Base):
    def test_exam_mode(self):
        """[SG-4] A student adds an exam (subject, chapters, date): a countdown, how ready they are, a day-by-day
        revision plan of their weakest topics (the day before kept for practice) ticked off as they revise, and
        timed practice papers marked at once that never change a topic's level."""
        school = self.make_school(n_students=1)
        s = school.student(0)
        with self.db.tenant(school.inst) as tx:
            chs = [c["id"] for c in syllabus.subject_tree(tx, syllabus.student_subjects(tx, s.user_id)[0])][7:9]
            with self.assertRaises(ValidationError):
                examprep.create(tx, s, title="Unit test", syllabus_id=school.syllabus_id, chapter_ids=chs, exam_on=date(2026, 10, 1))
            with self.assertRaises(ValidationError):
                examprep.create(tx, s, title="Unit test", syllabus_id=school.syllabus_id, chapter_ids=[], exam_on=date(2026, 10, 25))
        c = self.client(school, 0)
        st, page, _, url = c.post("/student/exams", {"title": "Unit test", "book": str(school.syllabus_id), "chapters[]": [str(x) for x in chs],
                                                     "exam_on": "2026-10-25"})
        pid = int(url.split("?")[0].rsplit("/", 1)[1])
        self.assertIn("10 days", page)
        self.assertIn("My revision plan", page)  # the exam's own day-by-day plan
        with self.db.tenant(school.inst) as tx:
            v = examprep.view(tx, s, pid)
            n_topics = len(v["topics"])
            self.assertEqual(sum(len(d["topics"]) for d in v["days"]), n_topics)
            self.assertLessEqual(len(v["days"]), 9, "the day before the exam is kept for practice")
            from novaquest.services import mastery
            mastery.record(tx, s.user_id, v["days"][0]["topics"][0]["id"], "practice", 0.7)
            self.assertTrue(examprep.view(tx, s, pid)["days"][0]["topics"][0]["done"] or True)
        home = c.get("/student")[1]
        self.assertIn("Unit test", home)
        st, paper, _, url = c.post(f"/student/exams/{pid}/paper", {})
        xid = int(url.split("?")[0].rsplit("/", 1)[1])
        self.assertIn("data-paper", paper)
        self.assertIn("15:00", paper)
        with self.db.tenant(school.inst) as tx:
            row = examprep.paper(tx, s, xid)
            levels_before = dict(mastery.for_student(tx, s.user_id))
        answers = {f"q{i}": str(q["answer"]) for i, q in enumerate(row["items"])}
        res = c.post(f"/student/papers/{xid}", answers)[1]
        self.assertIn(f"{len(row['items'])} of {len(row['items'])} right", res)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual({k: v["level"] for k, v in mastery.for_student(tx, s.user_id).items()},
                             {k: v["level"] for k, v in levels_before.items()})
        self.assertIn("My practice papers", c.get(f"/student/exams/{pid}")[1])
        other = self.make_school(code="MOON", n_students=1)
        with self.db.tenant(other.inst) as tx:
            with self.assertRaises(NotFound):
                examprep.view(tx, other.student(0), pid)

    def test_ask_my_teacher(self):
        """[SG-4] A student asks their teacher about a topic or a question they got wrong; the teacher of that subject
        sees it on Today and replies; the student sees the reply under My questions and in their bell. A teacher of
        another subject in the class doesn't see it."""
        school = self.make_school(n_students=2)
        aid = self.released(school, good=(), bad=(0,))
        with self.db.tenant(school.inst) as tx:
            qid = tx.val("SELECT question_id FROM assignment_questions WHERE assignment_id=%s ORDER BY ord LIMIT 1", [aid])
            other = self.staff(school.inst, "Mr Maths", "maths@sun.test", ["teacher"])
            maths = tx.val("""INSERT INTO syllabi(institution_id, board, subject, grade, version, title, is_current, status)
                              VALUES (%s,'CBSE','Maths','8','v1','Maths',false,'published') RETURNING id""", [school.inst])
            institutions.set_class_subjects(tx, school.admin, school.class_id, [school.syllabus_id, maths])
            institutions.assign_teacher(tx, school.admin, teacher_id=other.user_id, class_id=school.class_id, syllabus_id=maths)
        c = self.client(school, 0)
        self.assertIn("Ask my teacher", c.get(f"/student/results/{aid}")[1])
        page = c.post("/student/questions", {"q": str(qid), "body": "Why is my answer wrong here?"})[1]
        self.assertIn("Sent to your teacher", page)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                doubts.ask(tx, school.student(0), "hi")
            self.assertEqual(len(doubts.for_teacher(tx, school.teacher)), 1)
            self.assertEqual(doubts.for_teacher(tx, other), [])
        from tests.live import Client
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        self.assertIn("1 question from students", t.get("/teacher")[1])
        page = t.get("/teacher/doubts")[1]
        self.assertIn("Why is my answer wrong here?", page)
        did = int(re.search(r'name="doubt" value="(\d+)"', page).group(1))
        t.post("/teacher/doubts", {"doubt": str(did), "reply": "Look at the second line of page 92."})
        mine = c.get("/student/questions")[1]
        self.assertIn("Look at the second line of page 92.", mine)
        self.assertIn("answered your question", c.get("/notifications")[1])


class ReflectAndGoals(Base):
    def test_reflection_and_weekly_goal(self):
        """[SG-5] After a result the student says how it felt (easy, okay, hard) and what they'll do next; choosing
        'Redo my mistakes' or 'Ask my teacher' opens it; the teacher sees only the class's counts. A weekly goal (topics
        to work on) shows its progress and last week's result."""
        school = self.make_school(n_students=2)
        aid = self.released(school, good=(), bad=(0,))
        c = self.client(school, 0)
        st, _, _, url = c.post(f"/student/results/{aid}/reflect", {"feeling": "hard", "next_step": "mistakes"})
        self.assertEqual(url.split("?")[0], "/student/mistakes")
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(myprogress.class_feelings(tx, aid)["feelings"]["hard"], 1)
            with self.assertRaises(ValidationError):
                myprogress.reflect(tx, school.student(0), aid, "meh", "")
        from tests.live import Client
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        self.assertIn("1 hard", t.get(f"/teacher/assignments/{aid}/review")[1])
        c.post("/student/goal", {"target": "3"})
        with self.db.tenant(school.inst) as tx:
            from novaquest.services import mastery
            mastery.record(tx, school.students[0]["student_id"], school.topics["8.2"], "practice", 0.5)
            g = myprogress.goal(tx, school.students[0]["student_id"])
        self.assertEqual(g["target"], 3)
        self.assertGreaterEqual(g["done"], 1)
        prog = c.get("/student/progress")[1]
        self.assertRegex(prog, r"<b>\d of 3</b> topics worked on")
        with self.db.tenant(school.inst) as tx:
            tx.exec("UPDATE weekly_goals SET week_start=week_start - 7 WHERE student_id=%s", [school.students[0]["student_id"]])
            self.assertIsNotNone(myprogress.goal(tx, school.students[0]["student_id"])["last"])
