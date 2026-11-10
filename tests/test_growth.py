"""1.16.0: start-of-term and progress checks, and the growth they show."""
from __future__ import annotations

from datetime import timedelta

from novaquest.core import clock
from novaquest.core.errors import Conflict
from novaquest.services import assignments, attempts, growth, questions
from tests.base import PASSWORD, NQTestCase


class Growth(NQTestCase):
    def _schedule(self, school, aid):
        with self.db.tenant(school.inst) as tx:
            for q in questions.assignment_questions(tx, aid):
                if q["status"] == "draft":
                    questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                 expires_at=clock.now() + timedelta(hours=24), targets=[("class", school.class_id)])

    def _take(self, school, i, aid, right: int):
        """Student i answers the check, getting the first `right` questions right."""
        st = school.student(i)
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, st, aid)
            for k, q in enumerate(questions.assignment_questions(tx, aid)):
                good = q["answer"]["index"]
                attempts.save_answer(tx, st, att["id"], q["id"], {"index": good if k < right else (good + 1) % len(q["options"])})
        attempts.submit(self.db, self.engine, st, att["id"])

    def test_start_of_term_and_progress_checks_show_growth(self):
        """[DB-2][DB-3][AS-1] A teacher sets a class's start-of-term check for a subject: a multiple-choice quiz with
        questions from every chapter (up to 20), reviewed and scheduled like any assignment. A progress check covers the
        same topics with fresh questions. The Growth tab shows the class average then and now, the change, how many
        improved, each student's change and each chapter's; a PDF matches the screen; the principal's class page shows
        the growth in a line. A progress check needs a scheduled start-of-term check, and there's one start-of-term
        check per class and subject."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=3)
        sid = school.syllabus_id
        with self.assertRaises(Conflict):
            growth.create_check(self.db, self.engine, school.teacher, school.class_id, sid, "progress")
        base = growth.create_check(self.db, self.engine, school.teacher, school.class_id, sid, "baseline")
        with self.db.tenant(school.inst) as tx:
            a = assignments.get(tx, base)
            qs = questions.assignment_questions(tx, base)
        self.assertEqual((a["purpose"], a["kind"]), ("baseline", "quiz"))
        self.assertTrue(5 <= len(qs) <= 20)
        self.assertTrue(all(q["options"] for q in qs), "multiple choice only")
        self.assertGreaterEqual(len({q["topic_code"].split(".")[0] for q in qs}), 5, "questions from many chapters")
        self._schedule(school, base)
        with self.assertRaises(Conflict):
            growth.create_check(self.db, self.engine, school.teacher, school.class_id, sid, "baseline")
        n = len(qs)
        for i, right in enumerate((n // 4, n // 2, 0)):
            self._take(school, i, base, right)
        prog = growth.create_check(self.db, self.engine, school.teacher, school.class_id, sid, "progress")
        with self.db.tenant(school.inst) as tx:
            p = assignments.get(tx, prog)
            pqs = questions.assignment_questions(tx, prog)
        self.assertEqual((p["purpose"], p["baseline_id"], sorted(p["topic_ids"])), ("progress", base, sorted(a["topic_ids"])))
        self.assertTrue(p["title"].startswith("Progress check 1"))
        self.assertLess(len({x["prompt"] + str(x["options"]) for x in pqs} & {x["prompt"] + str(x["options"]) for x in qs}),
                        len(pqs), "fresh questions, not the same test again")
        self._schedule(school, prog)
        m = len(pqs)
        self._take(school, 0, prog, m)         # student 1: from about a quarter to all
        self._take(school, 1, prog, m // 4)    # student 2: down
        with self.db.tenant(school.inst) as tx:
            r = growth.report(tx, school.teacher, school.class_id, sid)
        sm = r["summary"]
        self.assertEqual(sm["n"], 2, "only students who took both count")
        self.assertEqual(sm["improved"], 1)
        st = {s["id"]: s for s in r["students"]}
        self.assertEqual(st[school.student(0).user_id]["latest"], 100)
        self.assertGreater(st[school.student(0).user_id]["change"], 0)
        self.assertLess(st[school.student(1).user_id]["change"], 0)
        self.assertIsNone(st[school.student(2).user_id]["latest"])
        self.assertTrue(r["chapters"] and all(c["baseline"] is not None for c in r["chapters"]))
        self.assertEqual([c["avg"] is not None for c in r["checks"]], [True, True])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/classes/{school.class_id}/growth")[1]
            for text in ("Start of term", f"{sm['baseline']}%", f"{sm['latest']}%", "Set a progress check", "By chapter"):
                self.assertIn(text, html)
            status, pdf, headers, _ = t.get(f"/teacher/classes/{school.class_id}/growth?book={sid}&format=pdf")
            self.assertEqual(headers["content-type"], "application/pdf")
            pr = Client(live.url)
            pr.login("principal@sun.test", PASSWORD)
            html = pr.get(f"/leader/classes/{school.class_id}")[1]
            self.assertIn("Learning growth", html)
            self.assertIn(f"({sm['change']:+d} points)", html)
            self.assertEqual(pr.get(f"/teacher/classes/{school.class_id}/growth")[0], 200, "the principal can open it")
            status, html, _, url = t.post(f"/teacher/classes/{school.class_id}/growth/new", {"book": str(sid), "purpose": "progress"})
            self.assertIn("Check drafted", html)
            self.assertIn("Progress check 2", html)
        finally:
            live.stop()
