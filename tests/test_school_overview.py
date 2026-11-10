"""1.30.0: the whole school at a glance for the principal and school admin, and the Green Valley demo school."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from novaquest.core.errors import Forbidden
from novaquest.services import principal
from novaquest.services.access import Actor
from tests.base import NQTestCase
from tools import demo_school


class DemoSchool(NQTestCase):
    def seed(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        info = demo_school.seed(self.db, storage=tmp)
        with self.db.platform() as tx:
            inst = tx.val("SELECT id FROM institutions WHERE school_code=%s", [demo_school.CODE])
        return info, inst

    def actor(self, inst: int, email: str) -> Actor:
        with self.db.tenant(inst) as tx:
            u = tx.one("""SELECT u.id, u.name, array_agg(r.role) AS roles FROM users u JOIN user_roles r ON r.user_id=u.id
                          WHERE u.email=%s GROUP BY u.id""", [email])
        return Actor(u["id"], inst, u["roles"][0], tuple(u["roles"]), u["name"])

    def test_demo_school(self):
        """[DM-1] One command makes a full demo school: five classes of 18 students, three subjects, three teachers (two
        with a timetable, one without), a principal and an admin, term plans, eight weeks of learning, and work at every
        stage (released, closed with answers to check, open, a draft meant for three sections, and work set for three
        sections together). The sign-ins are written out; running it twice is refused. Every main page works with it."""
        info, inst = self.seed()
        self.assertEqual(len(info["students"]), 5)
        self.assertTrue(all(len(v) == 18 and all(k["pin"] for k in v) for v in info["students"].values()))
        with self.db.tenant(inst) as tx:
            states = {r["state"]: r["n"] for r in tx.all("SELECT state, count(*) AS n FROM assignments GROUP BY 1")}
            self.assertTrue({"reviewed", "closed", "open", "draft"} <= set(states), states)
            self.assertEqual(tx.val("SELECT count(*) FROM assignments WHERE batch_id IS NOT NULL"), 3)
            self.assertEqual(tx.val("SELECT cardinality(sections) FROM assignments WHERE state='draft'"), 2)
            self.assertGreater(tx.val("SELECT count(*) FROM learn_events"), 1000)
            self.assertEqual(tx.val("SELECT count(DISTINCT teacher_id) FROM timetable_slots"), 2)
            self.assertGreater(tx.val("SELECT count(*) FROM responses WHERE flagged"), 0)
        with self.assertRaises(SystemExit):
            demo_school.seed(self.db)
        out = demo_school.write_logins(info, Path(tempfile.mkdtemp()) / "logins.txt")
        text = out.read_text()
        self.assertIn("priya@greenvalley.demo / GreenValley2026", text)
        self.assertIn("school code GVS-DEMO", text)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            for email, paths in (("priya@greenvalley.demo", ["/teacher", "/teacher/prep", "/teacher/plan", "/teacher/timetable",
                                                             "/teacher/assignments", "/teacher/assignments/new", "/teacher/classes"]),
                                 ("fatima@greenvalley.demo", ["/teacher", "/teacher/prep"]),
                                 ("principal@greenvalley.demo", ["/leader", "/leader?grade=9", "/leader?subject=Mathematics"]),
                                 ("admin@greenvalley.demo", ["/leader", "/admin"])):
                c = Client(live.url)
                c.login(email, info["password"])
                for p in paths:
                    st, body = c.get(p)[:2]
                    self.assertEqual(st, 200, f"{email} {p}")
                    self.assertNotIn("Traceback", body)
            c = Client(live.url)
            c.login("fatima@greenvalley.demo", info["password"])
            self.assertIn("Your classes", c.get("/teacher")[1])  # no timetable: every class she teaches
            k = info["students"]["8A"][0]
            s = Client(live.url)
            s.login_student(demo_school.CODE, "8A", k["roll"], k["pin"])
            self.assertEqual(s.get("/student")[0], 200)
        finally:
            live.stop()

    def test_school_overview(self):
        """[SD-1] The principal and the school admin see every class against every subject it studies (charts print each
        week's figure on the chart, with only the first, middle and last dates under it): understanding
        (with the 5-student rule), share of the book practised, the term plan (on plan, chapters behind), work set and
        handed in, and who teaches it; a summary by grade; students active each week; and each teacher's classes,
        work set, hand-in and lessons presented. Grade and subject filters narrow it; teachers can't open it."""
        info, inst = self.seed()
        head = self.actor(inst, "principal@greenvalley.demo")
        with self.db.tenant(inst) as tx:
            d = principal.school(tx, head)
            self.assertEqual([r["label"] for r in d["rows"]], ["8A", "8B", "8C", "9A", "9B"])
            self.assertEqual(d["subjects"], ["Mathematics", "Science"])
            self.assertEqual(d["grades"], ["8", "9"])
            self.assertNotIn("Mathematics", d["rows"][3]["cells"])  # 9A doesn't study the demo Maths book
            a8, c8 = d["rows"][0]["cells"]["Science"], d["rows"][2]["cells"]["Science"]
            self.assertGreater(a8["understanding"], c8["understanding"])
            self.assertEqual(a8["behind"], 0)
            self.assertGreater(c8["behind"], 0)
            self.assertIn("Priya", a8["teachers"])
            self.assertEqual([g["grade"] for g in d["by_grade"]], ["8", "9"])
            self.assertEqual(sum(g["students"] for g in d["by_grade"]), 90)
            self.assertGreaterEqual(len(d["active"]), 6)
            t = {x["name"]: x for x in d["teachers"]}
            self.assertGreater(t["Mrs Priya Rao"]["presented"], t["Ms Fatima Sheikh"]["presented"])
            self.assertEqual(t["Mrs Priya Rao"]["lessons_week"], 11)
            g9 = principal.school(tx, head, grade="9")
            self.assertEqual([r["label"] for r in g9["rows"]], ["9A", "9B"])
            m = principal.school(tx, head, subject="Mathematics")
            self.assertEqual([r["label"] for r in m["rows"]], ["8A", "8B", "8C"])
            self.assertEqual(m["subjects"], ["Mathematics"])
            admin = self.actor(inst, "admin@greenvalley.demo")
            self.assertEqual(len(principal.school(tx, admin)["rows"]), 5)
            with self.assertRaises(Forbidden):
                principal.school(tx, self.actor(inst, "priya@greenvalley.demo"))
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("principal@greenvalley.demo", info["password"])
            page = c.get("/leader?grade=8")[1]
            self.assertIn("Every class, every subject", page)
            self.assertIn("By grade", page)
            self.assertIn("Students active each week", page)
            self.assertIn('class="mini-chart"', page)  # 1.30.3: each week's figure printed above its bar
            self.assertEqual(page.count('class="ch-val"'), page.count('class="ch-bar"') + page.count('class="ch-dot"'))
            self.assertGreaterEqual(page.count('class="ch-bar"'), 6)
            self.assertLessEqual(page.count('class="ch-tick"'), 6, "only the first, middle and last dates under each chart")
            self.assertIn("Lessons presented", page)
            self.assertNotIn(">9A<", page)
            t = Client(live.url)
            t.login("priya@greenvalley.demo", info["password"])
            self.assertIn(t.get("/leader")[0], (403, 404))
        finally:
            live.stop()


class DemoSchoolOnAnyInstall(NQTestCase):
    def test_demo_without_the_ncert_book_and_after_a_failed_run(self):
        """[DM-1] The demo works on an install without NovaQuest's ready-made NCERT Science 8 book (it uploads a demo
        Science book instead), and if setting it up fails half-way the half-made school is set aside (code and
        sign-ins renamed, inactive) so the demo can simply be run again."""
        with self.db.platform() as tx:
            tx.exec("UPDATE syllabi SET is_current=false WHERE institution_id IS NULL")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        real = demo_school._book
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
            return real(*a, **k)

        demo_school._book = flaky
        try:
            with self.assertRaises(RuntimeError):
                demo_school.seed(self.db, storage=tmp)
        finally:
            demo_school._book = real
        with self.db.platform() as tx:
            gone = tx.one("SELECT school_code, status FROM institutions WHERE school_code LIKE %s", [demo_school.CODE + "-FAILED-%"])
            self.assertEqual(gone["status"], "inactive")
            self.assertFalse(tx.val("SELECT 1 FROM users WHERE email=%s", ["priya@greenvalley.demo"]))
        info = demo_school.seed(self.db, storage=tmp)
        with self.db.platform() as tx:
            inst = tx.val("SELECT id FROM institutions WHERE school_code=%s", [demo_school.CODE])
        with self.db.tenant(inst) as tx:
            books = {r["subject"] + r["grade"]: r["institution_id"] for r in tx.all(
                """SELECT DISTINCT b.subject, b.grade, b.institution_id FROM class_subjects cs JOIN syllabi b ON b.id=cs.syllabus_id""")}
            self.assertEqual(books["Science8"], inst, "the school's own demo Science book")
            self.assertGreater(tx.val("SELECT count(*) FROM assignments WHERE state='open'"), 0)
        self.assertEqual(len(info["students"]["9B"]), 18)

    def test_book_is_read_once_when_the_worker_reads_it_too(self):
        """[DM-1][SY-5] A book is read by one reader only: while another reader (NovaQuest's worker) is reading it, a
        second call does nothing, and a reader that stopped making progress for half an hour can be taken over. With
        NovaQuest running, the demo waits for the worker to finish reading each demo book before publishing it (this
        was 'Choose published books only' on a running install)."""
        import threading
        from datetime import timedelta

        from novaquest.ai.offline import OfflineEngine
        from novaquest.core import clock
        from novaquest.core.db import jsonb
        from novaquest.services import books

        school = self.make_school(n_students=1)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        from tools import demo_book
        pdf = demo_book.make(tmp / "b.pdf")
        with pdf.open("rb") as fh:
            sid = books.create(self.db, school.admin, board="CBSE", subject="Science", grade="8", title="Demo", files=[("b.pdf", fh)], storage=tmp)
        with self.db.tenant(school.inst) as tx:
            tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"stage": "reading", "reader_at": clock.now().isoformat()}), sid])
        books.read_book(self.db, OfflineEngine(), school.inst, sid, tmp)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT status FROM syllabi WHERE id=%s", [sid]), "processing", "someone else is reading it")
            tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s",
                    [jsonb({"reader_at": (clock.now() - timedelta(minutes=40)).isoformat()}), sid])
        books.read_book(self.db, OfflineEngine(), school.inst, sid, tmp)
        books.read_book(self.db, OfflineEngine(), school.inst, sid, tmp)  # a late second reader: nothing more
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT status FROM syllabi WHERE id=%s", [sid]), "review")
            self.assertEqual(tx.val("SELECT count(*) FROM syllabus_nodes WHERE syllabus_id=%s AND kind='chapter'", [sid]), 2)

        # the demo with a 'worker' that reads each book a moment later, while the demo's own read finds it claimed
        real = books.read_book

        def worker_reads(db, engine, inst, s, storage):
            with db.tenant(inst) as tx:
                tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"reader_at": clock.now().isoformat()}), s])
            threading.Timer(0.5, lambda: (self._clear(db, inst, s), real(db, engine, inst, s, storage))).start()

        books.read_book = worker_reads
        try:
            info = demo_school.seed(self.db, storage=tmp)
        finally:
            books.read_book = real
        self.assertEqual(len(info["students"]), 5)

    def _clear(self, db, inst, s):
        with db.tenant(inst) as tx:
            tx.exec("UPDATE syllabi SET meta = meta - 'reader_at' WHERE id=%s", [s])
