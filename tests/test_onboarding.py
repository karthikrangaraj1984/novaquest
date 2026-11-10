"""Institution and student onboarding (ON-1..ON-5, ST-1..ST-5)."""
from __future__ import annotations

import io
import time

import openpyxl

from novaquest.core import security
from novaquest.core.errors import Conflict, Forbidden, RateLimited, ValidationError
from novaquest.services import auth, institutions, pdfs, students, syllabus
from novaquest.services.access import Actor, visible_class_ids
from tests.base import PASSWORD, NQTestCase


class InstitutionOnboarding(NQTestCase):
    def test_platform_admin_creates_isolated_institution(self):
        """[ON-1] NovaQuest admin creates an institution with its own isolated data space and an invited admin."""
        pa = self.platform_admin()
        inst, invite = institutions.create_institution(self.db, pa, name="Sunrise School", kind="school", board="CBSE",
                                                       academic_year="2026-27", school_code="sun-cbe", admin_name="Asha",
                                                       admin_email="asha@sun.test", base_url="https://x")
        other, _ = institutions.create_institution(self.db, pa, name="Other", kind="college", board="Anna University",
                                                   academic_year="2026-27", school_code="OTH", admin_name="B",
                                                   admin_email="b@oth.test", base_url="https://x")
        with self.db.tenant(inst) as tx:
            self.assertEqual([r["name"] for r in tx.all("SELECT name FROM institutions")], ["Sunrise School"])
            self.assertEqual(tx.val("SELECT school_code FROM institutions"), "SUN-CBE")
            self.assertEqual(tx.val("SELECT count(*) FROM users"), 1)
        self.assertIn("/invite/", invite.link)
        with self.assertRaises(Conflict):
            institutions.create_institution(self.db, pa, name="Dup", kind="school", board="CBSE", academic_year="2026-27",
                                            school_code="SUN-CBE", admin_name="x", admin_email="x@y.test", base_url="https://x")
        with self.assertRaises(ValidationError):
            institutions.create_institution(self.db, pa, name="Bad", kind="school", board="CBSE", academic_year="2026-27",
                                            school_code="a b", admin_name="x", admin_email="x2@y.test", base_url="https://x")
        school = self.make_school("ZED")
        with self.assertRaises(Forbidden):
            institutions.create_institution(self.db, school.admin, name="Nope", kind="school", board="CBSE",
                                            academic_year="2026-27", school_code="NOPE", admin_name="x",
                                            admin_email="x3@y.test", base_url="https://x")

    def test_admin_sets_up_classes(self):
        """[ON-2] Institution admin sets up grade + section; classes appear for assigning teachers and students."""
        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            cid = institutions.create_class(tx, school.admin, grade="8", section="c", academic_year="2026-27")
            labels = [c["label"] for c in institutions.list_classes(tx)]
            self.assertIn("8C", labels)
            with self.assertRaises(Conflict):
                institutions.create_class(tx, school.admin, grade="8", section="C", academic_year="2026-27")
            with self.assertRaises(Forbidden):
                institutions.create_class(tx, school.teacher, grade="8", section="D", academic_year="2026-27")
            self.assertTrue(cid)

    def test_staff_invited_by_form_and_csv_and_can_log_in(self):
        """[ON-3] Admin creates teacher and leadership accounts by form or CSV; staff receive an invite and can log in."""
        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            inv = institutions.invite_staff(tx, school.admin, name="New Teacher", email="new@sun.test", roles=["teacher"],
                                            base_url="https://x")
            invites, errors = institutions.import_staff_csv(
                tx, school.admin, b"name,email,role\nDr Head,head@sun.test,principal\nT Two,t2@sun.test,teacher\nBad,not-an-email,teacher\n",
                "https://x")
            self.assertEqual(len(invites), 2)
            self.assertEqual(len(errors), 1)
            self.assertIn("Row 4", errors[0])
            emails = {r["to_addr"] for r in tx.all("SELECT to_addr FROM email_outbox")}
            self.assertTrue({"new@sun.test", "head@sun.test", "t2@sun.test"} <= emails)
        token = inv.link.rsplit("/", 1)[1]
        with self.assertRaises(ValidationError):
            auth.accept_invite(self.db, token, "short")
        auth.accept_invite(self.db, token, "GoodPassword1")
        sess = auth.login_email(self.db, self.settings, "new@sun.test", "GoodPassword1")
        self.assertEqual(sess.actor.role, "teacher")
        with self.assertRaises(ValidationError):
            auth.accept_invite(self.db, token, "GoodPassword1")  # one-time link
        lead = invites[0].link.rsplit("/", 1)[1]
        auth.accept_invite(self.db, lead, "HeadPassword1")
        self.assertEqual(auth.login_email(self.db, self.settings, "head@sun.test", "HeadPassword1").actor.role, "leader")

    def test_teacher_sees_only_mapped_classes(self):
        """[ON-4] Teachers are mapped to subject + class and see only their own classes."""
        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            other = institutions.create_class(tx, school.admin, grade="8", section="A", academic_year="2026-27")
            self.assertEqual(visible_class_ids(tx, school.teacher), [school.class_id])
            self.assertEqual(sorted(visible_class_ids(tx, school.leader)), sorted([school.class_id, other]))
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=other,
                                        syllabus_id=school.syllabus_id)
            self.assertEqual(sorted(visible_class_ids(tx, school.teacher)), sorted([school.class_id, other]))
            staff = {s["name"]: s for s in institutions.list_staff(tx)}
            self.assertIn("8A", staff["Mrs Rao"]["classes"])

    def test_rollover_moves_students_and_keeps_history(self):
        """[ON-5] Academic year rollover moves students to the next class with history and logins kept."""
        school = self.make_school(n_students=3)
        aid = self.open_assignment(school)
        from novaquest.services import attempts
        att = self.answer_all(school, school.student(0), aid)
        attempts.submit(self.db, self.engine, school.student(0), att)
        pin = school.students[0]["pin"]
        with self.db.tenant(school.inst) as tx:
            nxt = institutions.create_class(tx, school.admin, grade="9", section="B", academic_year="2027-28")
            moved = institutions.rollover(tx, school.admin, from_class_id=school.class_id, to_class_id=nxt)
            self.assertEqual(moved, 3)
            self.assertEqual(tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [nxt]), 3)
            self.assertEqual(tx.val("SELECT count(*) FROM attempts WHERE student_id=%s", [school.students[0]["student_id"]]), 1)
            self.assertEqual(tx.val("SELECT status FROM classes WHERE id=%s", [school.class_id]), "archived")
        sess = auth.login_pin(self.db, self.settings, "SUN", "9B", "1", pin)
        self.assertEqual(sess.actor.user_id, school.students[0]["student_id"])


class StudentOnboarding(NQTestCase):
    def test_forty_students_from_csv_in_under_a_minute_with_row_errors(self):
        """[ST-1] Teacher adds 40 students from one CSV in under a minute, with row-level error messages."""
        school = self.make_school(n_students=1)
        lines = ["roll_no,student_name,parent_email"] + [f"{i},Pupil {i},p{i}@mail.test" for i in range(2, 42)]
        lines += ["1,Someone Else,", "50,,x@y.test", "51,Typo Email,parent@gmial", "2,Dup In File,"]
        rows = students.parse_rows("\n".join(lines).encode(), "8B.csv")
        with self.db.tenant(school.inst) as tx:
            check = students.check_rows(tx, school.class_id, rows)
            msgs = {r.row: r.message for r in check.rows if r.status == "fix"}
            self.assertEqual(check.ready, 40)
            self.assertEqual(len(msgs), 4)
            self.assertIn("already used by Student 1", " ".join(msgs.values()))
            self.assertIn("Did you mean gmail.com?", " ".join(msgs.values()))
            self.assertIn("appears twice", " ".join(msgs.values()))
            self.assertIn("name is missing", " ".join(msgs.values()))
            with self.assertRaises(ValidationError):
                students.import_rows(tx, school.teacher, school.class_id, rows, skip_invalid=False)
        start = time.perf_counter()
        with self.db.tenant(school.inst) as tx:
            res = students.import_rows(tx, school.teacher, school.class_id, rows, skip_invalid=True)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(res.added), 40)
        self.assertLess(elapsed, 60)
        wb = openpyxl.Workbook()
        wb.active.append(["Roll No", "Student Name", "Parent Email"])
        wb.active.append([60, "Excel Pupil", ""])
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(students.parse_rows(buf.getvalue(), "list.xlsx"), [{"roll_no": "60", "name": "Excel Pupil", "parent_email": ""}])

    def test_login_slip_with_roll_and_pin(self):
        """[ST-2] Each student gets roll number + PIN; the teacher can print a login slip (PDF, 8 per page)."""
        school = self.make_school(n_students=9)
        pins = [s["pin"] for s in school.students]
        self.assertTrue(all(len(p) == 4 and p.isdigit() for p in pins))
        pdf = pdfs.login_slips("SUN", "8B", school.students)
        self.assertTrue(pdf.startswith(b"%PDF"))
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 2)
        text = reader.pages[0].extract_text()
        self.assertIn("Student 1", text)
        self.assertIn(" ".join(pins[0]), text)
        sess = auth.login_pin(self.db, self.settings, "sun", "8b", "1", pins[0])
        self.assertEqual(sess.actor.role, "student")
        with self.db.tenant(school.inst) as tx:
            stored = tx.val("SELECT pin_hash FROM enrolments WHERE roll_no='1'")
        self.assertNotIn(pins[0], stored)

    def test_pin_reset_allows_immediate_login(self):
        """[ST-3] Teacher resets a student's PIN; the student can log in again immediately, even after a lockout."""
        school = self.make_school(n_students=2)
        old = school.students[0]["pin"]
        for _ in range(self.settings.login_max_failures):
            with self.assertRaises(ValidationError):
                auth.login_pin(self.db, self.settings, "SUN", "8B", "1", "0000" if old != "0000" else "1111")
        with self.assertRaises(RateLimited):
            auth.login_pin(self.db, self.settings, "SUN", "8B", "1", old)
        with self.db.tenant(school.inst) as tx:
            new = students.reset_pin(tx, school.teacher, school.students[0]["enrolment_id"])
        self.assertEqual(auth.login_pin(self.db, self.settings, "SUN", "8B", "1", new).actor.user_id, school.students[0]["student_id"])
        if new != old:
            with self.assertRaises(ValidationError):
                auth.login_pin(self.db, self.settings, "SUN", "8B", "1", old)

    def test_account_needs_parental_consent(self):
        """[ST-4] A Grade 8 account cannot be used until parental consent is recorded."""
        school = self.make_school(n_students=1, consent=False)
        pin = school.students[0]["pin"]
        with self.assertRaises(Forbidden):
            auth.login_pin(self.db, self.settings, "SUN", "8B", "1", pin)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                students.record_consent(tx, school.teacher, school.students[0]["enrolment_id"], parent_name="", method="signed_form")
            students.record_consent(tx, school.teacher, school.students[0]["enrolment_id"], parent_name="Mr Parent",
                                    method="signed_form")
            self.assertEqual(tx.val("SELECT consent_by FROM enrolments"), "Mr Parent")
            self.assertTrue(tx.val("SELECT count(*) FROM audit_events WHERE action='student.consent'"))
        self.assertEqual(auth.login_pin(self.db, self.settings, "SUN", "8B", "1", pin).actor.role, "student")

    def test_deactivate_and_move_keep_history(self):
        """[ST-5] Teacher deactivates or moves a student; access ends (or follows the student) and history is kept."""
        school = self.make_school(n_students=3)
        sess = auth.login_pin(self.db, self.settings, "SUN", "8B", "1", school.students[0]["pin"])
        with self.db.tenant(school.inst) as tx:
            students.deactivate(tx, school.teacher, school.students[0]["enrolment_id"])
        self.assertIsNone(auth.get_session(self.db, sess.token))
        with self.assertRaises(ValidationError):
            auth.login_pin(self.db, self.settings, "SUN", "8B", "1", school.students[0]["pin"])
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM enrolments WHERE student_id=%s", [school.students[0]["student_id"]]), 1)
            other = institutions.create_class(tx, school.admin, grade="8", section="A", academic_year="2026-27")
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=other, syllabus_id=school.syllabus_id)
            students.move(tx, school.teacher, school.students[1]["enrolment_id"], to_class_id=other, new_roll_no="7")
            self.assertEqual(tx.val("SELECT count(*) FROM enrolments WHERE student_id=%s", [school.students[1]["student_id"]]), 2)
        self.assertEqual(auth.login_pin(self.db, self.settings, "SUN", "8A", "7", school.students[1]["pin"]).actor.role, "student")
