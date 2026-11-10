"""Data isolation, security, audit and privacy (NF-ISO, NF-SEC, NF-AUD, NF-PRIV)."""
from __future__ import annotations

import psycopg

from novaquest.core import security
from novaquest.core.errors import Forbidden, NotFound, RateLimited, ValidationError
from novaquest.services import attempts, auth, review, students
from tests.base import APP_URL, PASSWORD, NQTestCase
from tests.live import Client, LiveServer


class Isolation(NQTestCase):
    def test_database_enforces_isolation_on_every_tenant_table(self):
        """[NF-ISO] Each institution sees only its own data, enforced by Postgres row-level security on every table."""
        a = self.make_school("AAA")
        b = self.make_school("BBB")
        aid = self.open_assignment(a)
        att = self.answer_all(a, a.student(0), aid)
        attempts.submit(self.db, self.engine, a.student(0), att)
        with psycopg.connect(APP_URL) as conn:
            tables = [r[0] for r in conn.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity""").fetchall()]
        self.assertGreaterEqual(len(tables), 25)
        leaks = []
        with self.db.tenant(b.inst) as tx:
            for t in tables:
                col = "id" if t == "institutions" else "institution_id"
                n = tx.val(f'SELECT count(*) FROM "{t}" WHERE {col} = %s', [a.inst])
                if n:
                    leaks.append((t, n))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                tx.exec("INSERT INTO classes(institution_id, label, grade, section, academic_year) VALUES (%s,'X','8','X','y')", [a.inst])
        self.assertEqual(leaks, [])
        with self.db.anonymous() as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM users"), 0, "no tenant context, no rows")
        with psycopg.connect(APP_URL) as conn:
            self.assertFalse(conn.execute("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname='nq_app'").fetchone()[0])

    def test_services_refuse_other_schools_and_other_classes(self):
        """[NF-ISO] Services reject access to another school's or another class's records."""
        a = self.make_school("AAA")
        b = self.make_school("BBB")
        aid = self.open_assignment(a)
        with self.db.tenant(b.inst) as tx:
            with self.assertRaises(NotFound):
                review.queue(tx, b.teacher, aid)
            with self.assertRaises(NotFound):
                attempts.start_or_resume(tx, b.student(0), aid)
        from novaquest.services import institutions
        with self.db.tenant(a.inst) as tx:
            other = institutions.create_class(tx, a.admin, grade="8", section="Z", academic_year="2026-27")
            with self.assertRaises(Forbidden):
                students.add_student(tx, a.teacher, class_id=other, name="X", roll_no="1")


class Security(NQTestCase):
    def test_hashing_sessions_lockout_and_audit(self):
        """[NF-SEC] Passwords/PINs hashed with scrypt, sessions revocable, repeated failures locked out; [NF-AUD] logins audited."""
        h = security.hash_secret("1234")
        self.assertTrue(h.startswith("scrypt$") and "1234" not in h)
        self.assertTrue(security.verify_secret("1234", h) and not security.verify_secret("1235", h))
        self.assertIsNotNone(security.check_password_strength("password"))
        self.assertIsNone(security.check_password_strength("Better-Pass-2026"))
        school = self.make_school()
        for _ in range(self.settings.login_max_failures):
            with self.assertRaises(ValidationError):
                auth.login_email(self.db, self.settings, "rao@sun.test", "wrong")
        with self.assertRaises(RateLimited):
            auth.login_email(self.db, self.settings, "rao@sun.test", PASSWORD)
        self.clock.advance(minutes=self.settings.login_lock_minutes + 1)
        sess = auth.login_email(self.db, self.settings, "rao@sun.test", PASSWORD)
        self.assertIsNotNone(auth.get_session(self.db, sess.token))
        auth.logout(self.db, sess.token)
        self.assertIsNone(auth.get_session(self.db, sess.token))
        self.clock.advance(hours=13)
        s2 = auth.login_email(self.db, self.settings, "rao@sun.test", PASSWORD)
        self.clock.advance(hours=13)
        self.assertIsNone(auth.get_session(self.db, s2.token), "sessions expire")
        with self.db.tenant(school.inst) as tx:
            self.assertGreaterEqual(tx.val("SELECT count(*) FROM audit_events WHERE action='login.email'"), 2)
            with self.db.anonymous() as t2:
                self.assertEqual(t2.val("SELECT count(*) FROM sessions WHERE token_hash=%s", [sess.token]), 0, "only token hashes stored")

    def test_minimum_data_for_minors(self):
        """[NF-PRIV] Grade 8 students need no email or phone; only name, roll number and an optional parent email are stored."""
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            u = tx.one("SELECT email, password_hash FROM users WHERE id=%s", [school.students[0]["student_id"]])
        self.assertIsNone(u["email"])
        self.assertIsNone(u["password_hash"])
        with psycopg.connect(APP_URL) as conn:
            cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name IN ('users','enrolments')").fetchall()}
        self.assertFalse({"phone", "address", "date_of_birth", "dob", "aadhaar"} & cols)


class WebSecurity(NQTestCase):
    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def test_headers_csrf_and_role_checks_over_http(self):
        """[NF-SEC] Secure headers on every page, CSRF required for changes, roles and schools enforced on every URL."""
        a = self.make_school("AAA")
        b = self.make_school("BBB")
        aid = self.open_assignment(a)
        c = Client(self.live.url)
        status, _, h, _ = c.get("/login")
        self.assertEqual(status, 200)
        csp = h["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("app;dur=", h["Server-Timing"])
        status, text, _, _ = c.post("/login", {"email": "rao@aaa.test", "password": PASSWORD}, csrf=False)
        self.assertEqual(status, 403)
        c.login("rao@aaa.test", PASSWORD)
        status, _, _ = c.post_json(f"/api/attempts/1/answers", {"question_id": 1, "answer": {}}, csrf=False)
        self.assertIn(status, (401, 403))
        other = Client(self.live.url)
        other.login("rao@bbb.test", PASSWORD)
        self.assertEqual(other.get(f"/teacher/assignments/{aid}/review")[0], 404)
        self.assertEqual(other.get(f"/teacher/assignments/{aid}")[0], 404)
        st = Client(self.live.url)
        st.login_student("AAA", "8B", "1", a.students[0]["pin"])
        self.assertEqual(st.get("/teacher")[0], 403)
        self.assertEqual(st.get("/leader")[0], 403)
        self.assertEqual(c.get("/leader")[0], 403)
        anon = Client(self.live.url)
        self.assertEqual(anon.get("/teacher")[3], "/login")
        status, body, _ = anon.post_json("/api/learn/ask", {"question": "x"}, csrf=False)
        self.assertEqual(status, 401)
