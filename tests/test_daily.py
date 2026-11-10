"""1.17.0: Daily 5, a five-question spaced review a day."""
from __future__ import annotations

from datetime import timedelta

from novaquest.core.errors import Conflict
from novaquest.services import daily, learn
from tests.base import NQTestCase


class Daily5(NQTestCase):
    def test_spaced_review_moves_topics_by_how_well_they_went(self):
        """[LN-9][LN-3] Daily 5 brings back topics the student has started: five multiple-choice questions a day, made
        once and kept for the day, due topics first. A right answer moves a topic's next review further away (2, then
        4 days…), a wrong one brings it back tomorrow. It can be done once a day; the streak counts the days in a row.
        A student who hasn't started anything has nothing to review. No AI call."""
        school = self.make_school(n_students=2)
        st, fresh = school.student(0), school.student(1)
        with self.db.tenant(school.inst) as tx:
            self.assertIsNone(daily.today_review(tx, fresh))
            self.assertIsNone(daily.status(tx, fresh))
            a, b = school.topics["8.3"], school.topics["8.5"]
            for t in (a, b):
                learn.practice_submit(tx, st, t, {})
            row = daily.today_review(tx, st)
            items = row["items"]
            self.assertEqual(len(items), 5)
            self.assertEqual({i["topic_id"] for i in items}, {a, b})
            self.assertEqual(len({(i["prompt"], str(i["options"])) for i in items}), 5, "no question twice")
            self.assertEqual(daily.today_review(tx, st)["id"], row["id"], "the same five all day")
            answers = {i: (q["answer"] if q["topic_id"] == a else (q["answer"] + 1) % len(q["options"])) for i, q in enumerate(items)}
            res = daily.submit(tx, st, answers)
            self.assertEqual(res["right"], sum(1 for q in items if q["topic_id"] == a))
            self.assertEqual(res["streak"], 1)
            with self.assertRaises(Conflict):
                daily.submit(tx, st, answers)
            box = {r["topic_id"]: r for r in tx.all("SELECT topic_id, box, due_on FROM review_items WHERE student_id=%s", [st.user_id])}
            today = daily.today()
            self.assertEqual(box[b]["box"], 0)
            self.assertEqual(box[b]["due_on"], today + timedelta(days=1), "missed: back tomorrow")
            self.assertGreaterEqual(box[a]["box"], 1)
            self.assertGreater(box[a]["due_on"], today + timedelta(days=1), "right: later")
            self.assertEqual(daily.status(tx, st), {"done": True, "score": res["right"] / 5, "streak": 1})
        self.clock.advance(days=1)
        with self.db.tenant(school.inst) as tx:
            nxt = daily.today_review(tx, st)
            self.assertEqual(nxt["items"][0]["topic_id"], b, "tomorrow starts with the topic that was missed")
            self.assertEqual(daily.status(tx, st)["streak"], 1, "yesterday still counts until today is done")
            daily.submit(tx, st, {i: q["answer"] for i, q in enumerate(nxt["items"])})
            self.assertEqual(daily.streak(tx, st.user_id), 2)
        self.clock.advance(days=2)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(daily.streak(tx, st.user_id), 0, "a missed day ends the streak")

    def test_daily_5_over_http(self):
        """[LN-9][UX-1] Home shows the Daily 5 card (with the streak) once the student has started a topic; the page
        asks one question at a time; finishing shows what was right, the answer to anything missed with a link to
        revise it, and the card then says 'Done for today'."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        live = LiveServer(self.settings, self.db)
        try:
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            self.assertNotIn("Daily 5", s.get("/student")[1], "nothing started yet")
            with self.db.tenant(school.inst) as tx:
                learn.practice_submit(tx, school.student(0), school.topics["8.5"], {})
            html = s.get("/student")[1]
            self.assertIn("Daily 5", html)
            self.assertIn('href="/student/daily"', html)
            html = s.get("/student/daily")[1]
            self.assertIn("data-steps", html)
            self.assertEqual(html.count('class="qcard" data-step aria-labelledby="d'), 5)
            html = s.post("/student/daily", {"q0": "0"})[1]
            self.assertIn("of 5 right", html)
            self.assertIn("Answer:", html)
            self.assertIn("Revise", html)
            self.assertIn("Done for today", s.get("/student")[1])
            status, html, _, url = s.get("/student/daily")
            self.assertIn("done today", html.lower())
        finally:
            live.stop()
