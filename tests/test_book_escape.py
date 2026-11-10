"""1.12.0: escape rooms for every school book (Claude drafts, the admin approves), and the old NCERT Class 8 Science
book (2018) retired: no school is offered it or studies it."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from novaquest.core.errors import Conflict, NotFound
from novaquest.services import books, escape, institutions, review, syllabus
from tests.base import PASSWORD, ROOT, NQTestCase
from tests.test_books import made_up_book
from tests.test_escape import right_answer


def room(prefix: str) -> dict:
    """A drafted room for the made-up book's chapter, with a broken spare lock (dropped when checked)."""
    return {"title": "The Stuck Workshop", "story": "The workshop door is jammed and NOVA is muddled about machines. Fix its mistakes to get out.",
     "escaped": "The door swings open. NOVA now knows what makes a lever a lever.",
     "locks": [
      {"kind": "sort", "skill": "features", "topic": f"{prefix}.1", "title": "Sort the levers", "intro": "Choose the clues NOVA should use to spot levers.",
       "labels": ["Lever", "Not a lever"],
       "features": [{"key": "fulcrum", "text": "Turns about a fixed point"}, {"key": "metal", "text": "Made of metal"}, {"key": "handle", "text": "Has a handle"}],
       "train": [{"name": "Scissors", "label": 0, "f": {"fulcrum": 1, "metal": 1, "handle": 1}},
                 {"name": "Bottle opener", "label": 0, "f": {"fulcrum": 1, "metal": 1, "handle": 0}},
                 {"name": "Crowbar", "label": 0, "f": {"fulcrum": 1, "metal": 1, "handle": 0}},
                 {"name": "Ramp", "label": 1, "f": {"fulcrum": 0, "metal": 0, "handle": 0}},
                 {"name": "Rope", "label": 1, "f": {"fulcrum": 0, "metal": 0, "handle": 1}}],
       "test": [{"name": "Seesaw", "label": 0, "f": {"fulcrum": 1, "metal": 0, "handle": 1}},
                {"name": "Metal ramp", "label": 1, "f": {"fulcrum": 0, "metal": 1, "handle": 0}},
                {"name": "Wooden spoon lever", "label": 0, "f": {"fulcrum": 1, "metal": 0, "handle": 0}}],
       "hint": "What do all levers do?", "learn": "AI sorts by the clues it is given. 'Made of metal' fitted the examples by chance."},
      {"kind": "clean", "skill": "training", "topic": f"{prefix}.2", "title": "Fix NOVA's list", "intro": "Tick the examples labelled wrongly.",
       "rows": [{"item": "Seesaw", "label": "Lever"}, {"item": "Ramp", "label": "Lever", "wrong": True, "why": "a ramp is an inclined plane"},
                {"item": "Scissors", "label": "Lever"}, {"item": "Flag pulley", "label": "Pulley"}, {"item": "Well pulley", "label": "Pulley"},
                {"item": "Bottle opener", "label": "Pulley", "wrong": True, "why": "it turns about a fulcrum: a lever"}],
       "hint": "Does it turn about a fixed point?", "learn": "An AI copies its examples, mistakes included."},
      {"kind": "evidence", "skill": "checking", "topic": f"{prefix}.3", "title": "Check NOVA's claim", "intro": "Is NOVA right?",
       "claim": "A ramp lets you lift a load with more force.", "confidence": "92% sure", "verdict": 1,
       "evidence": ["A ramp is an inclined plane.", "An inclined plane lets us raise a load using less force over a longer distance.", "Pulleys use rope.", "Levers have a fulcrum."],
       "right": 1, "hint": "Look for the fact about force.", "learn": "Sounding sure is not being right."},
      {"kind": "pattern", "skill": "predicting", "topic": f"{prefix}.3", "title": "Predict the push", "intro": "NOVA has data about ramps.",
       "table": {"cols": ["Ramp length (m)", "Push needed (N)"], "rows": [["1", "100"], ["2", "50"], ["4", "25"]]},
       "question": "NOVA says a 100 m ramp needs no push at all. What is honest?",
       "options": ["NOVA is right.", "Longer ramps need less push, but NOVA shouldn't predict far beyond its data.", "Ramps don't help.", "Push doesn't depend on length."],
       "answer": 1, "hint": "How far beyond 4 m is 100 m?", "learn": "Don't predict far outside the data."},
      {"kind": "sort", "skill": "features", "topic": f"{prefix}.1", "title": "Broken spare", "intro": "x", "labels": ["A"], "features": [], "train": [], "test": [], "hint": "x", "learn": "x"}]}


class FakeClaude:
    """Drafts escape rooms like Claude: chapter 1's first draft can't be solved (a sorting lock whose clues never
    sort the new cases), so it goes back with the reason; the second draft is good."""
    name = "claude"

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def draft_escape_room(self, course, chapter_no, chapter_title, topics, text, problem=None):
        self.calls.append((chapter_no, problem))
        r = room(str(chapter_no))
        if chapter_no == 1 and problem is None:
            r["locks"][0]["test"][1] = {"name": "Plank", "label": 0, "f": {"fulcrum": 0, "metal": 0, "handle": 0}}  # = Ramp: unsolvable
            r["locks"] = [r["locks"][0], r["locks"][4]] + r["locks"][1:3]  # and no spare pattern lock to fall back on
        assert "[" in text and course.startswith("Grade 8 Science")
        return r


class BookEscapeRooms(NQTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-esc-"))
        self.storage = self.tmp / "books"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _own_book(self, school) -> int:
        from novaquest.ai.offline import OfflineEngine

        pdf = made_up_book(self.tmp / "physics.pdf")
        with pdf.open("rb") as fh:
            sid = books.create(self.db, school.admin, board="IGCSE", subject="Science", grade="8", title="Physics for You",
                               files=[(pdf.name, fh)], storage=self.storage)
        books.read_book(self.db, OfflineEngine(), school.inst, sid, self.storage)
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id,
                                        syllabus_id=sid)
        return sid

    def test_claude_drafts_rooms_for_a_school_book_the_admin_approves(self):
        """[LN-12][SY-5] A school's own book gets escape rooms: Claude drafts one per chapter from its text; each is
        checked like the built-in rooms (a draft whose sorting lock can't be solved goes back once with the reason; a
        broken spare lock is dropped). Drafts wait for the school admin, who checks each room with its answers and
        approves it; only then do students and teachers see it."""
        school = self.make_school()
        sid = self._own_book(school)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):  # not before the book is published
                tx.exec("UPDATE syllabi SET status='review' WHERE id=%s", [sid])
                books.request_escape(tx, school.admin, sid)
            tx.exec("UPDATE syllabi SET status='published' WHERE id=%s", [sid])
            books.request_escape(tx, school.admin, sid)
        engine = FakeClaude()
        books.draft_escape(self.db, engine, school.inst, sid)
        self.assertEqual([c for c, _ in engine.calls].count(1), 2, "chapter 1 went back once")
        self.assertIn("unsolvable", next(p for c, p in engine.calls if c == 1 and p))
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            d = books.detail(tx, sid)
            self.assertEqual({n: r["status"] for n, r in d["rooms"].items()}, {1: "draft", 2: "draft"})
            self.assertEqual(d["book"]["meta"]["escape"]["stage"], "ready")
            self.assertEqual(escape.student_rooms(tx, st), [], "drafts are hidden from students")
            self.assertEqual(escape.class_view(tx, school.teacher, school.class_id)["rooms"], [])
            room2 = escape.book_rooms(tx, syllabus.get_syllabus(tx, sid), approved_only=False)[2]
            self.assertEqual([lk["kind"] for lk in room2["locks"]], ["sort", "clean", "evidence", "pattern"])
            self.assertEqual(books.approve_escape(tx, school.admin, sid, 2), 1)
            rooms_ = escape.student_rooms(tx, st)
            self.assertEqual([(r["book_id"], r["chapter"]) for r in rooms_], [(sid, 2)])
            for n, lk in enumerate(room2["locks"], 1):
                self.assertTrue(escape.attempt(tx, st, 2, n, right_answer(lk), sid)["solved"])
            self.assertTrue(escape.open_door(tx, st, 2, escape.code_for(2, sid), sid))
            self.assertNotEqual(escape.code_for(2, sid), escape.code_for(2), "each book's rooms have their own codes")
            cv = escape.class_view(tx, school.teacher, school.class_id)
            self.assertEqual(cv["cells"][(st.user_id, sid, 2)], "escaped")
            self.assertEqual(cv["escapes"], 1)
            self.assertEqual(next(s for s in cv["skills"] if s["key"] == "features")["avg"], 100)
            g = escape.guide(tx, school.teacher, 2, sid)
            self.assertEqual(g["code"], escape.code_for(2, sid))
            self.assertIn("Made of metal", g["locks"][0]["trap"])
            with self.assertRaises(NotFound):
                escape.guide(tx, school.teacher, 1, sid)  # chapter 1 isn't approved yet
            with self.assertRaises(Conflict):
                books.discard_escape(tx, school.admin, sid, 2)  # played already
            books.discard_escape(tx, school.admin, sid, 1)
            self.assertNotIn(1, books.detail(tx, sid)["rooms"])

    def test_escape_rooms_over_http(self):
        """[LN-12][DB-8][UX-1] Over HTTP: the admin's book page offers 'Draft escape rooms', lists each drafted room
        to check, previews it with its answers and approves it; the student's Learn and Escape rooms pages link to
        the book's room, and the teacher's class page and answer guide show it."""
        from novaquest import worker
        from tests.live import Client, LiveServer

        school = self.make_school()
        sid = self._own_book(school)
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("Draft escape rooms with Claude (2 chapters)", html)
            books.draft_escape(self.db, FakeClaude(), school.inst, sid)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("To check", html)
            self.assertIn("Approve all 2", html)
            html = a.get(f"/admin/books/{sid}/escape/2")[1]
            self.assertIn("not yet approved", html)
            self.assertIn("Answer:", html)
            html = a.post(f"/admin/books/{sid}/escape_approve", {"chapter": "2"})[1]
            self.assertIn("Approved 1 escape room", html)
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = s.get(f"/student/learn?book={sid}")[1]
            self.assertIn(f'href="/student/escape/2?book={sid}"', html)
            html = s.get("/student/escape")[1]
            self.assertIn("The Stuck Workshop", html)
            html = s.get(f"/student/escape/2?book={sid}")[1]
            self.assertIn("Sort the levers", html)
            html = s.post(f"/student/escape/2?book={sid}", {"action": "try", "features[]": ["fulcrum"]})[1]
            self.assertIn("Lock 1 is open!", html)
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/classes/{school.class_id}/escape")[1]
            self.assertIn(f'href="/teacher/escape/2?book={sid}"', html)
            self.assertIn('title="1 of 4 locks"', html)
            html = t.get(f"/teacher/escape/2?book={sid}")[1]
            self.assertIn(escape.code_for(2, sid), html)
            self.assertIn("Science · IGCSE Grade 8", html)
        finally:
            live.stop()


class OldNcertRetired(NQTestCase):
    def test_retiring_the_2018_ncert_book(self):
        """[SY-6][SY-4] Migration 008 retires the out-of-date NCERT Class 8 Science (2018) book: it is archived, taken
        off every class (so no teacher can set new work on it), and never offered when the admin picks books (setup page, Books page) or given to a
        new class. Past work on it still opens. `nq init` no longer loads it."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=3)
        sql = (ROOT / "novaquest/core/migrations/008_book_escape_rooms.sql").read_text()
        retire = sql[sql.index("-- The NCERT Class 8 Science book of 2018"):]
        import psycopg

        from tests.base import OWNER_URL
        with psycopg.connect(OWNER_URL) as conn:
            conn.execute(retire)
            conn.commit()
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(syllabus.available_books(tx), [])
            self.assertEqual(syllabus.class_syllabi(tx, school.class_id), [])
            self.assertEqual(syllabus.teacher_syllabi(tx, school.teacher.user_id, school.class_id), [], "no new work on it")
            cid = institutions.create_class(tx, school.admin, grade="8", section="Z", academic_year="2026-27")
            self.assertEqual(syllabus.class_syllabi(tx, cid), [], "a new class starts with no book")
            self.assertEqual(escape.student_rooms(tx, school.student(0)), [])
            self.assertIsNotNone(review.class_summary(tx, aid), "past work on the old book still adds up")
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get("/admin")[1]
            self.assertNotRegex(html, r"NCERT Grade 8</label>|NCERT Grade 8</option>")
            self.assertIn("No published books yet", html)
            self.assertNotIn("Ready-made books", a.get("/admin/books")[1])
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            status, _, _, _ = t.get(f"/teacher/assignments/{aid}/review")
            self.assertEqual(status, 200, "past work on the old book still opens")
        finally:
            live.stop()
        nq = (ROOT / "tools" / "nq.py").read_text()
        self.assertNotIn("load_syllabus", nq)
        self.assertNotIn("ncert_science_class8_2018", nq)
