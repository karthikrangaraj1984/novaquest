"""1.19.0: data protection: consent register and withdrawal, export, erasure, retention, AI conversation log."""
from __future__ import annotations

import json

from novaquest.core.errors import Forbidden, ValidationError
from novaquest.services import attempts, auth, dashboards, learn, privacy, students, teach
from tests.base import PASSWORD, NQTestCase


class DataProtection(NQTestCase):
    def _busy(self, school, i=0):
        """A student who has done an assignment, asked NOVA and taught NOVA."""
        st = school.student(i)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=4)
        attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=True))
        learn.ask(self.db, self.engine, st, "What does the cell membrane do?", school.topics["8.5"])
        with self.db.tenant(school.inst) as tx:
            sid = teach.start(tx, st, school.topics["8.5"])["session"]["id"]
        teach.turn(self.db, self.engine, st, sid, "Only plant cells have a cell wall.")
        return st, aid

    def test_export_then_erase_keeps_class_figures(self):
        """[PR-2][PR-3] The school admin can export everything held about one student as one JSON file (profile,
        consent, answers, progress, Ask NOVA and Teach NOVA conversations), and erase a student on a parent's request
        by typing their name. Erasing removes the name, parent email, PIN, conversations and written answers, signs
        them out and blocks sign-in; marks stay without a name, so the class's figures don't change. Both are audited.
        Teachers can do neither."""
        school = self.make_school(n_students=3)
        st, aid = self._busy(school)
        with self.db.tenant(school.inst) as tx:
            data = privacy.export_student(tx, school.admin, st.user_id)
            text = json.dumps(data, default=str)
            self.assertEqual(data["student"]["name"], "Student 1")
            self.assertTrue(data["assignments"] and data["assignments"][0]["answers"])
            self.assertIn("What does the cell membrane do?", text)
            self.assertIn("Only plant cells have a cell wall.", text)
            self.assertEqual(data["enrolments"][0]["consent_status"], "recorded")
            self.assertNotIn("pin_hash", text)
            with self.assertRaises(Forbidden):
                privacy.export_student(tx, school.teacher, st.user_id)
            before = dashboards.assignment_results(tx, school.teacher, aid)["summary"]
            with self.assertRaises(ValidationError):
                privacy.erase_student(tx, school.admin, st.user_id, "Student 2")
            privacy.erase_student(tx, school.admin, st.user_id, "  student 1 ")
            self.assertEqual(tx.val("SELECT name FROM users WHERE id=%s", [st.user_id]), privacy.ERASED)
            self.assertIsNone(tx.val("SELECT parent_email FROM enrolments WHERE student_id=%s", [st.user_id]))
            for table in ("tutor_messages",):
                self.assertEqual(tx.val(f"SELECT count(*) FROM {table} WHERE student_id=%s", [st.user_id]), 0)
            self.assertEqual(tx.val("""SELECT count(*) FROM teach_turns t JOIN teach_sessions s ON s.id=t.session_id
                                       WHERE s.student_id=%s""", [st.user_id]), 0)
            self.assertEqual(tx.val("""SELECT count(*) FROM responses r JOIN attempts a ON a.id=r.attempt_id
                                       WHERE a.student_id=%s AND r.answer IS NOT NULL""", [st.user_id]), 0)
            after = dashboards.assignment_results(tx, school.teacher, aid)["summary"]
            self.assertEqual((before["science"], before["submitted"]), (after["science"], after["submitted"]))
            self.assertNotIn("Student 1", [r["name"] for r in privacy.consent_register(tx, school.admin)])
            self.assertEqual({a["action"] for a in tx.all("SELECT action FROM audit_events WHERE action LIKE 'privacy.%%'")},
                             {"privacy.export", "privacy.erase"})
            self.assertEqual(tx.val("SELECT pin_hash FROM enrolments WHERE student_id=%s", [st.user_id]), "!erased", "can't sign in")

    def test_withdraw_consent_retention_and_register(self):
        """[PR-1][PR-4] Withdrawing consent signs the student out and stops sign-in until consent is recorded again.
        The consent register lists every student with who gave consent, how and when, and downloads as CSV. Each
        day, conversations with NOVA older than the school's period are deleted, and students who left longer ago
        than the school's period are erased; 'keep' turns either off."""
        school = self.make_school(n_students=3)
        st, _ = self._busy(school)
        with self.db.tenant(school.inst) as tx:
            enr = tx.val("SELECT id FROM enrolments WHERE student_id=%s", [st.user_id])
            pin = students.reset_pin(tx, school.teacher, enr)
        auth.login_pin(self.db, self.settings, "SUN", "8B", "1", pin)
        with self.db.tenant(school.inst) as tx:
            privacy.withdraw_consent(tx, school.admin, enr)
            self.assertFalse(tx.val("SELECT count(*) FROM sessions WHERE user_id=%s AND NOT revoked", [st.user_id]), "signed out")
            row = next(r for r in privacy.consent_register(tx, school.admin) if r["student_id"] == st.user_id)
            self.assertEqual(row["consent_status"], "withdrawn")
        with self.assertRaises(Forbidden):
            auth.login_pin(self.db, self.settings, "SUN", "8B", "1", pin)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(row["consent_status"], "withdrawn")
            csv_text = privacy.register_csv(privacy.consent_register(tx, school.admin))
            self.assertIn("Class,Roll no,Student", csv_text)
            self.assertIn("8B,2,Student 2,yes,parent2@sun.test,recorded,Parent,Signed form", csv_text)
            leaver = school.student(2)
            students.deactivate(tx, school.teacher, tx.val("SELECT id FROM enrolments WHERE student_id=%s", [leaver.user_id]))
            self.assertEqual(privacy.settings(tx), {"ai_chat_months": 12, "left_student_months": 24})
            with self.assertRaises(ValidationError):
                privacy.save_settings(tx, school.admin, 5, 24)
            with self.assertRaises(Forbidden):
                privacy.save_settings(tx, school.teacher, 6, 12)
            privacy.save_settings(tx, school.admin, 6, 12)
            self.assertEqual(privacy.apply_retention(tx), {"messages": 0, "turns": 0, "photos": 0, "erased": 0})
        self.clock.advance(days=200)
        with self.db.tenant(school.inst) as tx:
            out = privacy.apply_retention(tx)
            self.assertGreater(out["messages"], 0)
            self.assertGreater(out["turns"], 0)
            self.assertEqual(out["erased"], 0, "left less than 12 months ago")
        self.clock.advance(days=200)
        self.assertEqual(privacy.daily_retention(self.db), 1)
        self.assertEqual(privacy.daily_retention(self.db), 0, "once a day")
        with self.db.tenant(school.inst) as tx:
            self.assertIsNotNone(tx.val("SELECT erased_at FROM users WHERE id=%s", [school.student(2).user_id]))
            self.assertIsNone(tx.val("SELECT erased_at FROM users WHERE id=%s", [school.student(1).user_id]), "still at school")
            privacy.save_settings(tx, school.admin, 0, 0)
        self.clock.advance(days=1)
        learn.ask(self.db, self.engine, school.student(1), "What is a cell?", school.topics["8.5"])
        self.clock.advance(days=1000)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(privacy.apply_retention(tx), {"messages": 0, "turns": 0, "photos": 0, "erased": 0}, "keep means keep")

    def test_pages_ai_log_and_parent_notice(self):
        """[PR-5][PR-6][UX-1] The principal (and school admin) can read students' conversations with NOVA, by class,
        period and words, and each look is audited; teachers can't. The admin's Data protection page shows the
        consent register, retention settings and recent actions, and gives a parent notice with a consent form as a
        PDF that reflects the school's settings."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=3)
        self._busy(school)
        with self.db.tenant(school.inst) as tx:
            log = privacy.ai_log(tx, school.leader)
            kinds = {c["kind"] for c in log["conversations"]}
            self.assertEqual(kinds, {"ask", "teach"})
            ask = next(c for c in log["conversations"] if c["kind"] == "ask")
            self.assertEqual((ask["student"], ask["class_label"]), ("Student 1", "8B"))
            self.assertEqual(ask["messages"][0], {**ask["messages"][0], "who": "student", "text": "What does the cell membrane do?"})
            self.assertEqual(len(privacy.ai_log(tx, school.leader, q="plant cells")["conversations"]), 1)
            self.assertEqual(privacy.ai_log(tx, school.leader, q="volcano")["conversations"], [])
            with self.assertRaises(Forbidden):
                privacy.ai_log(tx, school.teacher)
            self.assertEqual(tx.val("SELECT count(*) FROM audit_events WHERE action='privacy.ai_log_view'"), 3)
        live = LiveServer(self.settings, self.db)
        try:
            pr = Client(live.url)
            pr.login("principal@sun.test", PASSWORD)
            html = pr.get("/leader/ai-log")[1]
            for text in ("AI conversations", "Student 1", "Teach NOVA", "What does the cell membrane do?"):
                self.assertIn(text, html)
            self.assertIn("No conversations", pr.get("/leader/ai-log?q=volcano")[1])
            ad = Client(live.url)
            ad.login("admin@sun.test", PASSWORD)
            html = ad.get("/admin/privacy")[1]
            for text in ("Consent register", "How long we keep data", "Parent notice", "Withdraw consent", "Erase for good"):
                self.assertIn(text, html)
            status, _, headers, _ = ad.get("/admin/privacy/notice")
            self.assertEqual(headers["content-type"], "application/pdf")
            status, csv_text, headers, _ = ad.get("/admin/privacy?format=csv")
            self.assertIn("text/csv", headers["content-type"])
            status, body, headers, _ = ad.get(f"/admin/privacy/students/{school.student(0).user_id}/export")
            self.assertIn("application/json", headers["content-type"])
            self.assertIn("attachment", headers["content-disposition"])
            status, html, _, _ = ad.post("/admin/privacy/retention", {"ai_chat_months": "6", "left_student_months": "12"})
            self.assertIn("Saved", html)
            status, html, _, _ = ad.post(f"/admin/privacy/students/{school.student(2).user_id}/erase", {"confirm": "Student 3"})
            self.assertIn("erased", html)
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertEqual(t.get("/leader/ai-log")[0], 403)
            self.assertEqual(t.get("/admin/privacy")[0], 403)
        finally:
            live.stop()
