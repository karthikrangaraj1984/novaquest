"""Fixes from a Grade 8 Science teacher's review: fair marking that can't be gamed, stricter 'Mastered', the
question count asked for, understanding checks that stay on the topic, and following up on missing work."""
from __future__ import annotations

import re

from novaquest.ai.offline import OfflineEngine
from novaquest.core.errors import Conflict
from novaquest.services import attempts, mastery, notify, review
from novaquest.services.context import topic_ctx
from tests.base import PASSWORD, NQTestCase


class FairMarking(NQTestCase):
    def test_keyword_lists_are_capped_and_flagged_explanations_get_credit(self):
        """[NR-3][NR-4] Without Claude, a written answer that only lists the key words ('shape amoeba size') earns at
        most a third and is flagged for the teacher; an explanation in the student's own words earns the credit of
        the ideas it covers; a blank or 'not sure' answer earns nothing."""
        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            ctx = topic_ctx(tx, school.topics["8.3"])
        q = OfflineEngine().generate_questions([ctx], "short", "medium", 1, seed=1)[0]
        qd = {"kind": "short", "rubric": q.rubric, "answer": q.answer}
        for point in q.rubric:
            self.assertTrue(point["point"].startswith(("Explains: ", "Uses the idea")), point)
        self.assertTrue(any(len(p["any"]) > 1 for p in q.rubric), "an idea is credited by several of its key words")
        eng = OfflineEngine()
        words = " ".join(p["any"][0] for p in q.rubric)
        dump = eng.mark_answer(qd, {"text": words})
        self.assertLessEqual(dump.score / dump.max_score, 0.34, words)
        self.assertLess(dump.confidence, 0.7, "flagged for the teacher")
        self.assertIn("Only lists key words", dump.rationale)
        good = eng.mark_answer(qd, {"text": q.answer["model"] + " For example, a nerve cell is long because it carries messages."})
        self.assertGreater(good.score / good.max_score, 0.6)
        self.assertGreater(good.score, dump.score)
        unsure = eng.mark_answer(qd, {"text": "I am not sure."})
        self.assertEqual(unsure.score, 0)

    def test_gamed_answer_is_flagged_first_in_the_review(self):
        """[NR-4] In a real assignment, a key-word list goes to the teacher's 'To check' list instead of quietly
        getting full marks."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="short", topics=("8.3",), count=1)
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, st, aid)
            q = tx.one("""SELECT q.* FROM questions q JOIN assignment_questions aq ON aq.question_id=q.id
                          WHERE aq.assignment_id=%s""", [aid])
            attempts.save_answer(tx, st, att["id"], q["id"], {"text": " ".join(r["any"][0] for r in q["rubric"])})
        attempts.submit(self.db, self.engine, st, att["id"])
        with self.db.tenant(school.inst) as tx:
            r = tx.one("SELECT ai_score, max_score, flagged FROM responses WHERE attempt_id=%s", [att["id"]])
            self.assertTrue(r["flagged"])
            self.assertLessEqual(float(r["ai_score"]) / float(r["max_score"]), 0.34)
            self.assertEqual(review.queue(tx, school.teacher, aid)["to_check"], 1)


class StricterMastery(NQTestCase):
    def test_mastered_needs_two_kinds_of_evidence_and_can_slip_back(self):
        """[LN-5] One test, however good, makes a topic 'Practised', not 'Mastered'. Mastered needs the understanding
        check or Teach NOVA, or a strong test backed by other evidence. Each activity counts by its latest score, so
        a later poor result brings the level down."""
        school = self.make_school()
        sid, t = school.student(0).user_id, school.topics["8.5"]
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(mastery.record(tx, sid, t, "assignment", 0.95, replace_key="attempt:1"), 2)
            self.assertEqual(mastery.record(tx, sid, t, "check", 1.0), 3, "a strong test backed by a correct check")
            self.assertEqual(mastery.record(tx, sid, t, "check", 0.0), 2, "the latest check was wrong: back to Practised")
            self.assertEqual(mastery.record(tx, sid, t, "assignment", 0.9, replace_key="attempt:2"), 3, "two strong tests")
            other = school.topics["8.6"]
            self.assertEqual(mastery.record(tx, sid, other, "understanding", 0.8), 3)
            self.assertEqual(mastery.record(tx, sid, other, "understanding", 0.3), 1, "a poor second understanding check")
            self.assertEqual(mastery.record(tx, sid, other, "practice", 0.7), 2)


class QuestionCountAndTopic(NQTestCase):
    def test_count_asked_for_and_apply_questions_stay_on_topic(self):
        """[AS-1][AS-2] Asking for 5 written-answer or understanding-check questions on two topics gives 5 different
        questions; each understanding check's 'apply' part comes from its own topic, never a neighbouring one."""
        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            ctxs = [topic_ctx(tx, school.topics["2.3"]), topic_ctx(tx, school.topics["2.5"])]
        eng = OfflineEngine()
        for kind in ("short", "viva"):
            qs = eng.generate_questions(ctxs, kind, "medium", 5, seed=7)
            self.assertEqual(len(qs), 5, kind)
            self.assertEqual(len({q.prompt for q in qs}), 5, kind)
        own = {c.id: {m["example_prompt"] for m in c.misconceptions if m["topic"] == c.code} for c in ctxs}
        other = {c.id: {m["example_prompt"] for m in ctxs[1 - i].misconceptions} - own[c.id] for i, c in enumerate(ctxs)}
        for q in eng.generate_questions(ctxs, "viva", "medium", 6, seed=3):
            apply_part = q.prompt.split("Part 2 (apply): ")[1]
            self.assertNotIn(apply_part, other[q.topic_id], q.prompt)

    def test_teacher_is_told_when_fewer_questions_were_written(self):
        """[AS-1] Over HTTP: if NovaQuest can't write as many good questions as the teacher asked for, the draft says
        how many it made instead of silently making fewer."""
        from tests.live import Client, LiveServer

        live = LiveServer(self.settings, self.db)
        try:
            school = self.make_school()
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            t.get("/teacher/assignments/new")
            status, html, _, url = t.post("/teacher/assignments/new", {
                "for": f"{school.class_id}:{school.syllabus_id}", "topics[]": [school.topics["8.1"]], "kind": "quiz",
                "difficulty": "easy", "count": "20", "title": ""})
            self.assertEqual(status, 200)
            m = re.search(r"Draft ready with (\d+) of the 20 questions you asked for", html)
            self.assertTrue(m, url)
            self.assertLess(int(m.group(1)), 20)
        finally:
            live.stop()


class MissingWork(NQTestCase):
    def test_review_lists_everyone_missing_and_reminds_them(self):
        """[NR-2][SX-4] The review screen lists every student who hasn't handed in (not just the first three), and
        while the assignment is open the teacher can remind them all in one click; each gets one notice that
        refreshes if sent again. After closing, reminding is refused."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=6)
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        for i in range(2):
            att = self.answer_all(school, school.student(i), aid)
            attempts.submit(self.db, self.engine, school.student(i), att)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertIn("Not handed in yet (4)", html)
            for n in range(3, 7):
                self.assertIn(f"{n}. Student {n}", html)
            status, html, _, _ = t.post(f"/teacher/assignments/{aid}/remind")
            self.assertIn("Reminded 4 students", html)
            t.post(f"/teacher/assignments/{aid}/remind")
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(notify.unread_count(tx, school.students[3]["student_id"]), 1, "one notice, refreshed")
            self.assertEqual(notify.unread_count(tx, school.students[0]["student_id"]), 0, "not those who handed in")
            self.assertEqual(tx.val("SELECT count(*) FROM audit_events WHERE action='assignment.nudge'"), 2)
        self.clock.advance(days=2)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):
                review.nudge_missing(tx, school.teacher, aid)
