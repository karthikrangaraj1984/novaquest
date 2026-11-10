"""Assignment generation, review and scheduling (AS-1, AS-2, AS-4..AS-6, SC-1..SC-5)."""
from __future__ import annotations

from datetime import timedelta

from novaquest.core import clock
from novaquest.core.errors import Conflict, Forbidden, NotFound, ValidationError
from novaquest.services import assignments, attempts, questions
from tests.base import NQTestCase, timed


class Generation(NQTestCase):
    def test_pick_topics_difficulty_type_count_draft_under_60s(self):
        """[AS-1] Teacher picks topics, difficulty, type and number; a draft is generated in under 60 seconds."""
        school = self.make_school()
        aid, secs = timed(questions.generate_assignment, self.db, self.engine, school.teacher, class_id=school.class_id,
                          topic_ids=[school.topics["8.3"], school.topics["8.5"]], kind="quiz", difficulty="easy", count=8, title="")
        self.assertLess(secs, 60)
        with self.db.tenant(school.inst) as tx:
            a = assignments.get(tx, aid)
            qs = questions.assignment_questions(tx, aid)
        self.assertEqual(a["state"], "draft")
        self.assertEqual(len(qs), 8)
        self.assertTrue(all(q["status"] == "draft" and q["difficulty"] == "easy" for q in qs))
        self.assertTrue(a["title"].startswith("Quiz"))
        for bad in [dict(kind="essay"), dict(difficulty="extreme"), dict(topic_ids=[]), dict(count=50)]:
            args = dict(class_id=school.class_id, topic_ids=[school.topics["8.3"]], kind="quiz", difficulty="easy", count=5, title="")
            args.update(bad)
            with self.assertRaises(ValidationError):
                questions.generate_assignment(self.db, self.engine, school.teacher, **args)

    def test_all_types_for_grade8(self):
        """[AS-2] Mission (game-style), quiz, short answer, understanding check (viva), Learn and Teach NOVA can all be generated."""
        school = self.make_school()
        seen = {}
        for kind in questions.KINDS:
            aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                                topic_ids=[school.topics["8.5"], school.topics["8.6"]], kind=kind, difficulty="medium",
                                                count=5, title="")
            with self.db.tenant(school.inst) as tx:
                seen[kind] = [q["kind"] for q in questions.assignment_questions(tx, aid)]
        self.assertTrue(set(seen["mission"]) >= {"mcq", "ai_moment", "short"})
        self.assertEqual(set(seen["quiz"]), {"mcq"})
        self.assertEqual(set(seen["short"]), {"short"})
        self.assertEqual(set(seen["viva"]), {"viva"})
        self.assertEqual(seen["learn"], ["learn", "learn"])
        self.assertEqual(seen["teach"], ["teach", "teach"])
        with self.db.tenant(school.inst) as tx:
            mission = tx.all("SELECT scenario FROM questions WHERE kind <> 'learn' AND scenario LIKE 'Mission stage%%'")
        self.assertTrue(mission)

    def test_every_question_tagged_with_outcome_level_and_source(self):
        """[AS-4] Every question carries a learning outcome, a thinking level and its syllabus source."""
        school = self.make_school()
        for kind, diff in [("quiz", "easy"), ("quiz", "hard"), ("short", "medium"), ("viva", "medium")]:
            aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                                topic_ids=[school.topics["8.5"], school.topics["11.8"]], kind=kind, difficulty=diff,
                                                count=4, title="")
            with self.db.tenant(school.inst) as tx:
                for q in questions.assignment_questions(tx, aid):
                    self.assertTrue(q["outcome"])
                    self.assertIn(q["thinking_level"], ("remember", "apply", "analyse"))
                    self.assertTrue(q["citation"] and q["citation"][0]["ref"].startswith("NCERT Science Class VIII"))

    def test_review_edit_regenerate_remove_then_publish(self):
        """[AS-5] Teacher reviews each question: approve, edit, regenerate or remove; nothing reaches students unapproved."""
        school = self.make_school()
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[school.topics["8.3"], school.topics["8.5"]], kind="mission", difficulty="medium",
                                            count=6, title="Draft")
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
            with self.assertRaises(ValidationError):
                assignments.schedule(tx, school.teacher, aid, opens_at=clock.now(), expires_at=clock.now() + timedelta(days=1),
                                     targets=[("class", school.class_id)])
            mcq = next(q for q in qs if q["kind"] == "mcq")
            questions.edit(tx, school.teacher, mcq["id"], prompt="Which part controls the cell?", options=["Nucleus", "Vacuole", "Wall"],
                           answer_index=0, model_answer=None, rubric_points=None)
            questions.remove(tx, school.teacher, qs[-1]["id"])
        new_id = questions.regenerate(self.db, self.engine, school.teacher, qs[1]["id"])
        with self.db.tenant(school.inst) as tx:
            now_qs = questions.assignment_questions(tx, aid)
            self.assertIn(new_id, [q["id"] for q in now_qs])
            self.assertNotIn(qs[-1]["id"], [q["id"] for q in now_qs])
            self.assertEqual(next(q for q in now_qs if q["id"] == mcq["id"])["status"], "approved")
            for q in now_qs:
                if q["status"] == "draft":
                    questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now(), expires_at=clock.now() + timedelta(days=1),
                                 targets=[("class", school.class_id)])
            with self.assertRaises(Conflict):
                questions.approve(tx, school.teacher, new_id)
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, school.student(0), aid)
            view = attempts.view(tx, school.student(0), att["id"])
        self.assertEqual(len(view["questions"]), len(now_qs))
        self.assertNotIn("answer", view["questions"][0])

    def test_question_bank_reuse_without_ai(self):
        """[AS-6] Approved questions are saved to a bank; a teacher builds an assignment from it with no AI call."""
        school = self.make_school()
        self.open_assignment(school, kind="quiz", count=5)

        class NoAI:
            def __getattr__(self, name):
                raise AssertionError("AI must not be called")
        with self.db.tenant(school.inst) as tx:
            bank = questions.bank(tx)
            self.assertGreaterEqual(len(bank), 5)
            aid = questions.create_from_bank(tx, school.teacher, class_id=school.class_id, question_ids=[b["id"] for b in bank[:3]],
                                             title="Bank quiz")
            qs = questions.assignment_questions(tx, aid)
            self.assertEqual(len(qs), 3)
            self.assertTrue(all(q["status"] == "approved" and q["origin"] == "bank" for q in qs))
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now(), expires_at=clock.now() + timedelta(hours=2),
                                 targets=[("class", school.class_id)])
        self.assertIsNotNone(NoAI)


class Scheduling(NQTestCase):
    def test_assign_to_class_group_or_students(self):
        """[SC-1] Assign to a whole class, a group or individual students; only assigned students see it."""
        school = self.make_school(n_students=4)
        s = [school.students[i]["student_id"] for i in range(4)]
        with self.db.tenant(school.inst) as tx:
            gid = assignments.create_group(tx, school.teacher, school.class_id, "Needs help", [s[0], s[1]])
        a_group = self.open_assignment(school, targets=[("group", gid)])
        a_one = self.open_assignment(school, targets=[("student", s[3])])
        with self.db.tenant(school.inst) as tx:
            seen = {i: {a["id"] for a in assignments.for_student(tx, s[i])} for i in range(4)}
            self.assertEqual(seen[0], {a_group})
            self.assertEqual(seen[2], set())
            self.assertEqual(seen[3], {a_one})
            with self.assertRaises(NotFound):
                attempts.start_or_resume(tx, school.student(2), a_group)

    def test_open_and_expiry_times_in_ist(self):
        """[SC-2] Open and expiry date-times (Indian time): invisible as open before opening, closed after expiry."""
        school = self.make_school()
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[school.topics["8.5"]], kind="quiz", difficulty="easy", count=3, title="")
        opens = clock.parse_ist("2026-10-15T14:00")
        closes = clock.parse_ist("2026-10-16T18:00")
        self.assertEqual(opens.hour, 8)  # 14:00 IST is 08:30 UTC
        with self.db.tenant(school.inst) as tx:
            for q in questions.assignment_questions(tx, aid):
                questions.approve(tx, school.teacher, q["id"])
            self.assertEqual(assignments.schedule(tx, school.teacher, aid, opens_at=opens, expires_at=closes,
                                                  targets=[("class", school.class_id)]), "scheduled")
            self.assertEqual(assignments.for_student(tx, school.students[0]["student_id"])[0]["view"], "upcoming")
            from novaquest.core.errors import Forbidden as F
            with self.assertRaises(F):
                attempts.start_or_resume(tx, school.student(0), aid)
        self.clock.set(opens + timedelta(minutes=1))
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(assignments.for_student(tx, school.students[0]["student_id"])[0]["view"], "open")
            attempts.start_or_resume(tx, school.student(0), aid)
        self.clock.set(closes + timedelta(minutes=1))
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(assignments.for_student(tx, school.students[1]["student_id"])[0]["view"], "closed")
            with self.assertRaises(F):
                attempts.start_or_resume(tx, school.student(1), aid)
            with self.assertRaises(ValidationError):
                aid2 = questions.create_from_bank(tx, school.teacher, class_id=school.class_id,
                                                  question_ids=[q["id"] for q in questions.bank(tx)][:1], title="x")
                assignments.schedule(tx, school.teacher, aid2, opens_at=closes, expires_at=opens, targets=[("class", school.class_id)])

    def test_time_limit_and_attempts_enforced(self):
        """[SC-3] Optional time limit and number of attempts are enforced."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", count=3, time_limit_min=20, max_attempts=2)
        st = school.student(0)
        att = self.answer_all(school, st, aid)
        with self.db.tenant(school.inst) as tx:
            row = tx.one("SELECT started_at, deadline_at FROM attempts WHERE id=%s", [att])
        self.assertEqual(row["deadline_at"] - row["started_at"], timedelta(minutes=20))
        self.clock.advance(minutes=21)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):
                attempts.save_answer(tx, st, att, tx.val("SELECT question_id FROM assignment_questions WHERE assignment_id=%s LIMIT 1", [aid]), {"index": 0})
        from novaquest import worker
        worker.tick(self.db, self.settings)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT status FROM attempts WHERE id=%s", [att]), "submitted")
            second = attempts.start_or_resume(tx, st, aid)
            self.assertEqual(second["attempt_no"], 2)
        attempts.submit(self.db, self.engine, st, second["id"])
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):
                attempts.start_or_resume(tx, st, aid)

    def test_extend_deadline_for_all_or_one(self):
        """[SC-4] Teacher extends the deadline for everyone or one student; the new expiry applies immediately."""
        school = self.make_school(n_students=3)
        aid = self.open_assignment(school, kind="quiz", count=3, hours=2)
        self.clock.advance(hours=3)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Forbidden):
                attempts.start_or_resume(tx, school.student(0), aid)
            assignments.extend(tx, school.teacher, aid, clock.now() + timedelta(hours=5), school.students[0]["student_id"])
            self.assertTrue(attempts.start_or_resume(tx, school.student(0), aid))
            with self.assertRaises(Forbidden):
                attempts.start_or_resume(tx, school.student(1), aid)
            assignments.extend(tx, school.teacher, aid, clock.now() + timedelta(days=1))
            self.assertTrue(attempts.start_or_resume(tx, school.student(1), aid))
            self.assertEqual(assignments.get(tx, aid)["live_state"], "open")
            with self.assertRaises(ValidationError):
                assignments.extend(tx, school.teacher, aid, clock.now() - timedelta(hours=1))

    def test_states_draft_scheduled_open_closed_reviewed(self):
        """[SC-5] Assignment states draft → scheduled → open → closed → reviewed are shown on the teacher's list."""
        school = self.make_school()
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[school.topics["8.5"]], kind="quiz", difficulty="easy", count=3, title="")

        def state() -> str:
            with self.db.tenant(school.inst) as tx:
                return next(a for a in assignments.for_teacher(tx, school.teacher, [school.class_id]) if a["id"] == aid)["live_state"]
        self.assertEqual(state(), "draft")
        with self.db.tenant(school.inst) as tx:
            for q in questions.assignment_questions(tx, aid):
                questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() + timedelta(hours=1),
                                 expires_at=clock.now() + timedelta(hours=3), targets=[("class", school.class_id)])
        self.assertEqual(state(), "scheduled")
        self.clock.advance(hours=2)
        self.assertEqual(state(), "open")
        att = self.answer_all(school, school.student(0), aid)
        attempts.submit(self.db, self.engine, school.student(0), att)
        self.clock.advance(hours=2)
        self.assertEqual(state(), "closed")
        from novaquest.services import review
        with self.db.tenant(school.inst) as tx:
            closed = assignments.sync_states(tx)
            self.assertEqual(closed, [aid])
            review.release(tx, school.teacher, aid)
        self.assertEqual(state(), "reviewed")
