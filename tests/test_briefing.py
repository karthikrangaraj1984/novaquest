"""1.18.0: the weekly class briefing for teachers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from novaquest.core.errors import Forbidden
from novaquest.services import attempts, briefing, mastery
from tests.base import PASSWORD, NQTestCase


class WeeklyBriefing(NQTestCase):
    def _week(self):
        """Six students: two weak on one assignment with a mix-up, one who was active before and stopped, one whose
        practice scores fell, and one who didn't hand in work that closed this week."""
        school = self.make_school(n_students=6)
        s = [school.student(i) for i in range(6)]
        with self.db.tenant(school.inst) as tx:
            for st in (s[3], s[4]):  # three weeks ago: active and doing well
                for _ in range(3):
                    mastery.record(tx, st.user_id, school.topics["8.2"], "practice", 0.9)
        self.clock.advance(days=10)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=4, hours=48)
        for st, good in ((s[0], False), (s[1], False), (s[2], True)):
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=good))
        with self.db.tenant(school.inst) as tx:
            mis = tx.one("SELECT code, label, topic_id FROM misconceptions WHERE topic_id=%s LIMIT 1", [school.topics["8.5"]])
            tx.exec("""UPDATE responses SET misconception_codes=ARRAY[%s] WHERE attempt_id IN
                       (SELECT id FROM attempts WHERE student_id = ANY(%s))""", [mis["code"], [s[0].user_id, s[1].user_id]])
            for _ in range(2):
                mastery.record(tx, s[4].user_id, school.topics["8.4"], "practice", 0.3)
        self.clock.advance(days=3)  # the assignment has closed; s[3], s[4] and s[5] never handed it in
        return school, s, mis, aid

    def test_briefing_finds_mixups_hard_topics_and_students_slipping(self):
        """[DB-2][DB-1] The briefing covers the last seven days: the mix-up most students showed, topics the class
        averaged under 60% on, and each student to check on with plain reasons (missed work that closed this week,
        scores falling against their usual level, no activity after being active). A 10-minute reteach activity
        for the biggest mix-up is built from the mix-up, the lesson and an approved question. Only the class's
        teachers and leaders see it. No AI call."""
        school, s, mis, _ = self._week()
        with self.db.tenant(school.inst) as tx:
            b = briefing.weekly(tx, school.teacher, school.class_id)
            self.assertEqual(b["mixups"][0]["code"], mis["code"])
            self.assertEqual(b["mixups"][0]["students"], 2)
            self.assertTrue(b["hard"] and all(int(h["pct"]) < 60 for h in b["hard"]), b["hard"])
            why = {x["id"]: x["reasons"] for x in b["slipping"]}
            self.assertNotIn(s[2].user_id, why, "did the work well")
            self.assertTrue(any(r.startswith("Didn't hand in Test task") for r in why[s[5].user_id]))
            self.assertIn("No activity this week (active the weeks before)", why[s[3].user_id])
            self.assertTrue(any(r.startswith("Scores fell to 30% this week (usually 90%)") for r in why[s[4].user_id]), why[s[4].user_id])
            self.assertEqual(len(b["slipping"][0]["reasons"]), max(len(r) for r in why.values()), "most reasons first")
            f = b["focus"]
            self.assertEqual(f["topic_id"], mis["topic_id"])
            self.assertEqual(f["minutes"], 10)
            self.assertEqual([x["name"] for x in f["steps"]], ["Hook", "Put it right", "Check", "Exit ticket"])
            self.assertIn("Write on the board", f["steps"][0]["text"])
            self.assertTrue(f["steps"][2]["options"])
            self.assertEqual(b["activity"]["students"], 6)
            other = self.staff(school.inst, "Mr Other", "other@sun.test", ["teacher"])
            with self.assertRaises(Forbidden):
                briefing.weekly(tx, other, school.class_id)
            briefing.weekly(tx, school.leader, school.class_id)
        self.clock.advance(days=8)
        with self.db.tenant(school.inst) as tx:
            later = briefing.weekly(tx, school.teacher, school.class_id)
            self.assertEqual(later["mixups"], [], "only the last seven days")
            self.assertFalse(later["focus"])

    def test_monday_briefing_once_and_page(self):
        """[NR-1][UX-1] From Monday 7 am (India) each teacher gets the briefing for each class once, as a bell
        notification and an email linking to the Briefing tab; the tab shows the reteach activity, mix-ups and
        students to check on, and downloads as a PDF."""
        from tests.live import Client, LiveServer

        from novaquest import worker

        school, s, mis, _ = self._week()  # Wed 28 Oct 2026
        self.clock.set(datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))  # Monday 26 Oct, 5:30 am IST: not yet
        self.assertEqual(worker.weekly_briefings(self.db), 0)
        self.clock.set(datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc))  # Monday 7:30 am IST: the week with the quiz
        self.assertEqual(worker.weekly_briefings(self.db), 1)
        self.assertEqual(worker.weekly_briefings(self.db), 0, "once a week")
        self.assertEqual(worker.weekly_summaries(self.db), 1, "the principal's summary is separate")
        self.clock.set(datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc))  # back to Wednesday, for the page
        with self.db.tenant(school.inst) as tx:
            note = tx.one("SELECT title, body, link FROM notifications WHERE user_id=%s AND dedupe_key LIKE 'briefing:%%'",
                          [school.teacher.user_id])
            self.assertEqual(note["title"], "Weekly briefing: class 8B")
            self.assertIn(mis["label"], note["body"])
            self.assertEqual(note["link"], f"/teacher/classes/{school.class_id}/briefing")
            self.assertTrue(tx.val("SELECT count(*) FROM email_outbox WHERE subject LIKE 'NovaQuest weekly briefing%%'"))
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            html = c.get(f"/teacher/classes/{school.class_id}/briefing")[1]
            for text in ("10-minute reteach", mis["label"], "Students to check on", 'aria-current="page">Briefing'):
                self.assertIn(text, html)
            status, _, headers, _ = c.get(f"/teacher/classes/{school.class_id}/briefing?format=pdf")
            self.assertEqual(headers["content-type"], "application/pdf")
            self.assertTrue(c.last_bytes.startswith(b"%PDF"))
            self.assertIn(f"/teacher/classes/{school.class_id}/briefing", c.get("/teacher")[1])
        finally:
            live.stop()
