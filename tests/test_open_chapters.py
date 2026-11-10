"""1.28.0: teachers open chapters for self-study (whole chapters, only some, or with the term plan), escape rooms follow
their chapter, and an escape room can be set as an assignment."""
from __future__ import annotations

from datetime import timedelta

from novaquest.core import clock
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import assignments, attempts, escape, learn, plans, questions
from tests.base import PASSWORD, NQTestCase
from tests.test_escape import right_answer


class OpenChapters(NQTestCase):
    def _chapters(self, school):
        with self.db.tenant(school.inst) as tx:
            return {r["code"]: r["id"] for r in tx.all("SELECT id, code FROM syllabus_nodes WHERE syllabus_id=%s AND kind='chapter'",
                                                        [school.syllabus_id])}

    def _visible(self, school):
        with self.db.tenant(school.inst) as tx:
            return {c["no"] for c in learn.topics_for_student(tx, school.student(0))}

    def test_modes_and_whole_chapters(self):
        """[OC-1] A teacher sets how the class's book opens for self-study: every chapter open (as before), closed until
        they open chapters, or each chapter on its start date in the term plan. Whole chapters open or lock in one
        click, or 'open only these' opens the ticked chapters and locks the rest. A chapter opened or locked by hand
        always wins over the term plan. Closed topics' lessons can't be opened; work set on them still can."""
        school = self.make_school(n_students=1)
        ch = self._chapters(school)
        t = school.teacher
        all_chapters = self._visible(school)
        self.assertIn("8", all_chapters)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                learn.set_self_study_mode(tx, t, school.class_id, [school.syllabus_id], "sometimes")
            learn.set_self_study_mode(tx, t, school.class_id, [school.syllabus_id], "closed")
        self.assertEqual(self._visible(school), set())
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(NotFound):
                learn.lesson(tx, school.student(0), school.topics["8.3"])
            learn.set_chapters_open(tx, t, school.class_id, [ch["8"]], True)
        self.assertEqual(self._visible(school), {"8"})
        with self.db.tenant(school.inst) as tx:
            learn.open_only(tx, t, school.class_id, [school.syllabus_id], [ch["9"], ch["10"]])
        self.assertEqual(self._visible(school), {"9", "10"})
        # with the term plan: a chapter opens on its start date; one locked by hand stays locked
        with self.db.tenant(school.inst) as tx:
            tx.exec("DELETE FROM topic_settings WHERE class_id=%s", [school.class_id])
            learn.set_self_study_mode(tx, t, school.class_id, [school.syllabus_id], "plan")
            today = clock.to_ist(clock.now()).date()
            plans.save(tx, t, school.class_id, school.syllabus_id, [(ch["8"], today - timedelta(days=3), today + timedelta(days=3)),
                                                                    (ch["9"], today + timedelta(days=7), today + timedelta(days=12))])
        self.assertEqual(self._visible(school), {"8"})
        self.clock.advance(days=8)
        self.assertEqual(self._visible(school), {"8", "9"})
        with self.db.tenant(school.inst) as tx:
            learn.set_chapters_open(tx, t, school.class_id, [ch["9"]], False)
        self.assertEqual(self._visible(school), {"8"})
        with self.db.tenant(school.inst) as tx:
            learn.set_chapters_open(tx, t, school.class_id, [ch["8"]], False)
        self.assertEqual(self._visible(school), set())
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=2)  # work set on a closed chapter
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, school.student(0), aid)
            self.assertTrue(attempts.view(tx, school.student(0), att["id"])["questions"])

    def test_syllabus_page_controls(self):
        """[OC-1][UX-1] On Syllabus each chapter shows Open, Locked or how many topics are open, with Open chapter /
        Lock chapter; above it, how the book opens for this class and 'Open only some chapters'."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        ch = self._chapters(school)
        key = f"{school.class_id}:{school.syllabus_id}"
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            page = c.get("/teacher/syllabus")[1]
            self.assertIn("What students can study on their own", page)
            self.assertIn("Lock chapter", page)
            c.post("/teacher/syllabus/chapters", {"for": key, "action": "mode", "mode": "closed"})
            page = c.get("/teacher/syllabus")[1]
            self.assertIn("Open chapter", page)
            self.assertIn('<span class="pill warn">Locked</span>', page)
            c.post("/teacher/syllabus/chapters", {"for": key, "action": "open", "chapter_id": str(ch["8"])})
            self.assertEqual(self._visible(school), {"8"})
            c.post("/teacher/syllabus/chapters", {"for": key, "action": "only", "chapters[]": [str(ch["10"]), str(ch["11"])]})
            self.assertEqual(self._visible(school), {"10", "11"})
        finally:
            live.stop()


class EscapeRoomWork(NQTestCase):
    def test_rooms_follow_their_chapter_and_can_be_set_as_work(self):
        """[OC-2] A chapter's escape room shows only while its chapter is open for the class. A teacher can set a
        chapter's room as an assignment (from New assignment or 'Set as assignment' on Escape rooms) for the class, a
        group or chosen students, with open and close dates: it appears in their Tasks and opens the room even if the
        chapter is closed. Each lock opened is saved as progress; escaping hands it in (score 100%), and at the deadline
        the share of locks opened counts. A chapter without an approved room can't be set."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            learn.set_self_study_mode(tx, school.teacher, school.class_id, [school.syllabus_id], "closed")
            self.assertEqual(escape.student_rooms(tx, st), [])
            with self.assertRaises(NotFound):
                escape.view(tx, st, 8)
        topics_8 = [v for k, v in school.topics.items() if k.startswith("8.")]
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id, topic_ids=topics_8[:1],
                                            kind="escape", difficulty="medium", count=6, title="")
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
            self.assertEqual([(q["kind"], q["status"]) for q in qs], [("escape", "approved")])
            a = assignments.get(tx, aid)
            self.assertTrue(a["title"].startswith("Escape room: Ch 8"))
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                 expires_at=clock.now() + timedelta(days=2), targets=[("student", st.user_id)])
            self.assertEqual([r["chapter"] for r in escape.student_rooms(tx, st)], [8], "set as work: open for them")
            self.assertEqual(escape.student_rooms(tx, school.student(1)), [], "but not for the others")
        with self.assertRaises(ValidationError):
            questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                          topic_ids=[school.topics["8.3"], next(v for k, v in school.topics.items() if k.startswith("9."))],
                                          kind="escape", difficulty="medium", count=1, title="")
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertIn("kind=escape", t.get(f"/teacher/classes/{school.class_id}/escape")[1])
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            self.assertIn("Escape room", s.get("/student/tasks")[1])
            _, _, _, url = s.get(f"/student/assignments/{aid}")
            self.assertTrue(url.startswith("/student/escape/8"), url)
            room = escape.get_room(8)
            for n, lk in enumerate(room["locks"], 1):
                with self.db.tenant(school.inst) as tx:
                    escape.attempt(tx, st, 8, n, right_answer(lk))
                    if n == 2:
                        escape.progress_assignments(tx, st, school.syllabus_id, 8)
                        saved = tx.val("""SELECT r.answer FROM responses r JOIN attempts t ON t.id=r.attempt_id
                                          WHERE t.assignment_id=%s""", [aid])
                        self.assertEqual(saved, {"done": False, "score": 0.5})
            s.post("/student/escape/8", {"action": "door", "code": escape.code_for(8)})
            for path in (f"/teacher/assignments/{aid}", f"/teacher/assignments/{aid}/review"):
                status, html, _, _ = t.get(path)
                self.assertEqual(status, 200, path)
                self.assertIn("Escape room", html)
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            att = tx.one("SELECT id, status FROM attempts WHERE assignment_id=%s", [aid])
            self.assertEqual(att["status"], "submitted")
            self.assertEqual(float(tx.val("SELECT ai_score FROM responses WHERE attempt_id=%s", [att["id"]])), 1.0)
