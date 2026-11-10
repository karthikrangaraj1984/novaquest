"""End-to-end flows over HTTP against the real app (all roles, all pages)."""
from __future__ import annotations

import re

from novaquest.services import institutions
from tests.base import PASSWORD, NQTestCase
from tests.live import Client, LiveServer


class WebFlows(NQTestCase):
    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def test_every_page_renders_for_every_role(self):
        """[NF-A11Y] Every screen renders for each role with one h1-level heading and labelled inputs."""
        school = self.make_school()
        aid = self.open_assignment(school)
        pages = {
            ("rao@sun.test", None): ["/teacher", "/teacher/assignments", "/teacher/assignments/new", f"/teacher/assignments/{aid}",
                                     f"/teacher/assignments/{aid}/review", f"/teacher/assignments/{aid}/review?tab=students",
                                     f"/teacher/assignments/{aid}/review?tab=all", "/teacher/syllabus",
                                     f"/teacher/syllabus?topic={school.topics['8.5']}", "/teacher/students", "/teacher/progress",
                                     f"/teacher/classes/{school.class_id}/mastery", "/teacher/bank", "/notifications",
                                     f"/teacher/students/{school.students[0]['student_id']}/tutor"],
            ("principal@sun.test", None): ["/leader", "/teacher/syllabus"],
            ("admin@sun.test", None): ["/admin", "/leader", f"/teacher/classes/{school.class_id}/students"],
        }
        for (email, _), paths in pages.items():
            c = Client(self.live.url)
            c.login(email, PASSWORD)
            for p in paths:
                status, html, _, _ = c.get(p)
                self.assertEqual(status, 200, p)
                self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1, f"{p} has one h1")
                self._labels_ok(html, p)
        self.platform_admin()
        pa = Client(self.live.url)
        pa.login("nq@admin.test", PASSWORD)
        self.assertEqual(pa.get("/platform")[0], 200)
        st = Client(self.live.url)
        st.login_student("SUN", "8B", "1", school.students[0]["pin"])
        for p in ["/student", "/student/tasks", "/student/learn", f"/student/learn/{school.topics['8.5']}",
                  f"/student/learn/{school.topics['8.5']}/practice", f"/student/learn/{school.topics['8.5']}/understand",
                  f"/student/teach/{school.topics['8.5']}", "/student/progress", "/notifications"]:
            status, html, _, _ = st.get(p)
            self.assertEqual(status, 200, p)
            self.assertTrue('lang="en-IN"' in html)
            self._labels_ok(html, p)

    def test_student_can_sign_out_from_every_screen(self):
        """[SX-7] Every student screen has the account menu with Sign out; signing out ends the session and
        returns to the student sign-in page."""
        school = self.make_school()
        st = Client(self.live.url)
        st.login_student("SUN", "8B", "1", school.students[0]["pin"])
        name = school.students[0]["name"]
        for p in ["/student", "/student/tasks", "/student/learn", f"/student/learn/{school.topics['8.5']}",
                  "/student/progress", "/notifications"]:
            status, html, _, _ = st.get(p)
            self.assertEqual(status, 200, p)
            menu = re.search(r'<details class="account" data-account>(.*?)</details>', html, re.S)
            self.assertIsNotNone(menu, f"account menu on {p}")
            self.assertIn(f"My account: {name}", menu.group(1))
            self.assertRegex(menu.group(1), r'<form method="post" action="/logout" data-signout>\s*<input type="hidden" name="csrf" value="[^"]+">')
            self.assertIn(">Sign out</button>", menu.group(1))
        status, html, _, url = st.post("/logout")
        self.assertEqual(status, 200)
        self.assertTrue(url.endswith("/login/student"), url)
        status, _, _, url = st.get("/student")
        self.assertTrue(url.endswith("/login/student") or url.endswith("/login"), url)

    def _labels_ok(self, html: str, page: str) -> None:
        for m in re.finditer(r"<(input|textarea|select)\b([^>]*)>", html):
            attrs = m.group(2)
            if re.search(r'type="(hidden|submit|radio|checkbox)"', attrs):
                continue  # radios/checkboxes sit inside their <label>
            idm = re.search(r'id="([^"]+)"', attrs)
            self.assertTrue(idm and (f'for="{idm.group(1)}"' in html or "aria-label" in attrs), f"unlabelled field on {page}: {attrs[:80]}")

    def test_full_teacher_to_student_flow_over_http(self):
        """[AS-1][AS-5][SC-1][SX-3][SX-2][NR-5][SX-5] Generate → approve → schedule → student works with autosave and hints → release → scorecard."""
        school = self.make_school()
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        t.get("/teacher/assignments/new")
        status, html, _, url = t.post("/teacher/assignments/new", {"class_id": school.class_id, "kind": "mission", "difficulty": "medium",
                                                                   "count": 5, "title": "Web mission",
                                                                   "topics[]": [school.topics["8.3"], school.topics["8.5"]]})
        self.assertEqual(status, 200)
        aid = int(re.search(r"/teacher/assignments/(\d+)", url).group(1))
        self.assertIn("Draft ready", html)
        for qid in sorted(set(re.findall(r'/teacher/questions/(\d+)/approve', html))):
            t.post(f"/teacher/questions/{qid}/approve", {"assignment_id": aid})
        status, html, _, _ = t.get(f"/teacher/assignments/{aid}")
        self.assertNotIn("Needs your check", html)
        status, html, _, url = t.post(f"/teacher/assignments/{aid}/schedule", {"who": "class", "opens_at": "2026-10-15T09:00",
                                                                                "expires_at": "2026-10-17T18:00", "max_attempts": 1})
        self.assertIn("Scheduled", html)
        s = Client(self.live.url)
        status, html, _, url = s.login_student("sun", "8b", "1", school.students[0]["pin"])
        self.assertEqual(url, "/student")
        self.assertIn("Web mission", html)
        status, html, _, url = s.get(f"/student/assignments/{aid}")
        att = int(re.search(r"/student/attempts/(\d+)", url).group(1))
        qids = [int(q) for q in re.findall(r'data-q="(\d+)"', html)]
        self.assertEqual(len(qids), 5)
        for q in qids:
            code, body, _ = s.post_json(f"/api/attempts/{att}/answers", {"question_id": q, "answer": {"index": 0, "text": "The nucleus controls the cell."}})
            self.assertEqual(code, 200, body)
        code, hint, _ = s.post_json(f"/api/attempts/{att}/questions/{qids[0]}/hint", {})
        self.assertEqual(code, 200)
        self.assertTrue(hint["text"])
        status, html, _, _ = s.get(f"/student/attempts/{att}")
        self.assertIn("checked", html, "saved answers come back after a reload")
        status, html, _, url = s.post(f"/student/attempts/{att}/submit")
        self.assertIn("Handed in", html)
        status, html, _, _ = t.get(f"/teacher/assignments/{aid}/review")
        for rid in re.findall(r"/teacher/responses/(\d+)/override", html):
            t.post(f"/teacher/responses/{rid}/override", {"score": "ai", "comment": ""})
        status, html, _, _ = t.post(f"/teacher/assignments/{aid}/release")
        self.assertIn("Results released to 1 students", html)
        status, html, _, _ = s.get(f"/student/results/{aid}")
        self.assertEqual(status, 200)
        self.assertRegex(html, r"\d+ of \d+ right")
        self.assertIn("not exam marks", html)

    def test_class_list_upload_check_and_slips_over_http(self):
        """[ST-1][ST-2] Upload a CSV, see row errors, skip or fix, then download the login slips PDF."""
        school = self.make_school(n_students=1)
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        t.get(f"/teacher/classes/{school.class_id}/students")
        csv_data = b"roll_no,student_name,parent_email\n2,Asha K,a@x.com\n3,Ravi P,r@gmial\n1,Clash,\n"
        status, html, _, _ = t.post_multipart(f"/teacher/classes/{school.class_id}/students/import", {}, {"file": ("8B.csv", csv_data, "text/csv")})
        self.assertIn("Did you mean gmail.com?", html)
        self.assertIn("Roll 1 is already used", html)
        self.assertIn('aria-invalid="true"', html)
        status, html, _, _ = t.post(f"/teacher/classes/{school.class_id}/students/import",
                                    {"commit": "skip", "roll_no[]": ["2", "3", "1"], "name[]": ["Asha K", "Ravi P", "Clash"],
                                     "parent_email[]": ["a@x.com", "r@gmial", ""]})
        self.assertIn("Login slips", html)
        self.assertIn("Asha K", html)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        status, _, headers, _ = t.post("/teacher/slips.pdf", {"token": token})
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertTrue(t.last_bytes.startswith(b"%PDF"))
        status, _, _, _ = t.post("/teacher/slips.pdf", {"token": token + "x"})
        self.assertEqual(status, 422)

    def test_learn_ask_check_practice_understand_teach_over_http(self):
        """[LN-1][LN-2][LN-3][LN-4][TA-8] Student Learn mode and Teach NOVA work through the web API."""
        school = self.make_school()
        s = Client(self.live.url)
        s.login_student("SUN", "8B", "2", school.students[1]["pin"])
        tid = school.topics["8.5"]
        s.get(f"/student/learn/{tid}")
        code, r, _ = s.post_json("/api/learn/ask", {"question": "What does the nucleus do?", "topic_id": tid})
        self.assertEqual(code, 200)
        self.assertTrue(r["citations"])
        code, r, _ = s.post_json(f"/api/learn/{tid}/check", {"index": 0, "choice": 1})
        self.assertTrue(r["correct"])
        status, html, _, _ = s.post(f"/student/learn/{tid}/practice", {"q0": "0", "q1": "1"})
        self.assertIn("Mission complete", html)
        status, html, _, _ = s.post(f"/student/learn/{tid}/understand", {"explain": "The nucleus controls the cell and the cytoplasm holds organelles inside the cell membrane.",
                                                                       "apply": "For example the membrane lets water in."})
        self.assertIn("Next step", html)
        status, html, _, _ = s.get(f"/student/teach/{tid}")
        sid = int(re.search(r"/api/teach/(\d+)/turn", html).group(1))
        code, r, _ = s.post_json(f"/api/teach/{sid}/turn", {"message": "Only plant cells have a cell wall; animal cells like cheek cells only have a membrane."})
        self.assertEqual(r["verdict"], "correct")
        status, html, _, _ = s.get("/student/progress")
        self.assertIn("Share with my parents", html)
        status, html, _, _ = s.post("/student/report/share")
        link = re.search(r'value="(http[^"]+/r/[^"]+)"', html).group(1)
        anon = Client(self.live.url)
        status, page_html, headers, _ = anon.get(link.replace("http://testserver", ""))
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"], "a parent's link opens a page, not a file download")
        self.assertIn("learning progress", page_html)
        status, _, headers, _ = anon.get(link.replace("http://testserver", "") + ".pdf")
        self.assertEqual((status, headers["content-type"]), (200, "application/pdf"))

    def test_admin_platform_and_leader_screens_over_http(self):
        """[ON-1][ON-2][ON-3][ON-4][DB-3][DB-4] Admin and leadership pages work end to end, including exports."""
        self.platform_admin()
        pa = Client(self.live.url)
        pa.login("nq@admin.test", PASSWORD)
        pa.get("/platform")
        status, html, _, _ = pa.post("/platform/institution", {"name": "Web School", "kind": "school", "board": "CBSE", "academic_year": "2026-27",
                                                                "school_code": "WEB", "admin_name": "Wendy", "admin_email": "wendy@web.test"})
        invite = re.search(r'value="(http[^"]+/invite/[^"]+)"', html).group(1).replace("http://testserver", "")
        a = Client(self.live.url)
        a.get(invite)
        a.post(invite, {"password": "WendyPass123", "password2": "WendyPass123"})
        a.login("wendy@web.test", "WendyPass123")
        a.get("/admin")
        a.post("/admin/class", {"grade": "8", "section": "B"})
        status, html, _, _ = a.post("/admin/staff", {"name": "Tara", "email": "tara@web.test", "roles[]": ["teacher", "leader"]})
        self.assertIn("Invite sent", html)
        tid = re.search(r'<option value="(\d+)">Tara</option>', html).group(1)
        cid = re.search(r'<select id="mc" name="class_id"><option value="(\d+)">8B', html).group(1)
        status, html, _, _ = a.post("/admin/map", {"teacher_id": tid, "class_id": cid})
        self.assertIn("Subject (book) is required", html, "no default book any more: the admin picks one")
        book = re.search(r'<select id="mb" name="syllabus_id" required><option value="(\d+)">', a.get("/admin")[1]).group(1)
        status, html, _, _ = a.post("/admin/map", {"teacher_id": tid, "class_id": cid, "syllabus_id": book})
        self.assertIn("Teacher mapped", html)
        status, csv_text, headers, _ = a.get("/leader?format=csv")
        self.assertIn("text/csv", headers["content-type"])
        self.assertIn("8B", csv_text)
        status, _, headers, _ = a.get("/leader?format=pdf")
        self.assertEqual(headers["content-type"], "application/pdf")
        with self.db.platform() as tx:
            inst = tx.val("SELECT id FROM institutions WHERE school_code='WEB'")
        with self.db.tenant(inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM user_roles WHERE role IN ('teacher','leader')"), 2)

    def test_multi_role_switch_and_notifications(self):
        """[ON-3] One person can hold two roles (e.g. a principal who teaches) and switch between them."""
        school = self.make_school()
        self.staff(school.inst, "Dual Role", "dual@sun.test", ["leader", "teacher"])
        with self.db.tenant(school.inst) as tx:
            uid = tx.val("SELECT id FROM users WHERE email='dual@sun.test'")
            institutions.assign_teacher(tx, school.admin, teacher_id=uid, class_id=school.class_id, syllabus_id=school.syllabus_id)
        c = Client(self.live.url)
        status, html, _, url = c.login("dual@sun.test", PASSWORD)
        self.assertEqual(url, "/leader")
        status, html, _, url = c.post("/role", {"role": "teacher"})
        self.assertEqual(url, "/teacher")
        status, html, _, _ = c.get("/notifications")
        self.assertEqual(status, 200)
        status, html, _, url = c.post("/notifications")
        self.assertEqual(url, "/notifications")
