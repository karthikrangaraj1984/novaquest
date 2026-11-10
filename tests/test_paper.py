"""1.21.0: marking work done on paper, from photos."""
from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
from pathlib import Path

from novaquest.core.errors import Conflict, ValidationError
from novaquest.services import attempts, maths, paper, privacy, questions, review
from tests.base import PASSWORD, NQTestCase

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class PaperWork(NQTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def _setup(self):
        school = self.make_school(n_students=3)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=2)
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
        return school, aid, qs

    def test_teacher_types_answers_and_marks_without_claude(self):
        """[PW-1][PW-3] Without Claude, the teacher uploads a student's photos, types the answers from them next to the
        photos, and marks: the work is marked like online work (source: paper) and appears in the results. Only
        photos or PDF scans are accepted, at most 6 pages; a student who handed in online can't also get paper
        work; marked work can't be marked twice."""
        school, aid, qs = self._setup()
        st = school.student(0)
        with self.assertRaises(ValidationError):
            paper.upload(self.db, school.teacher, aid, st.user_id, [("notes.txt", io.BytesIO(b"hello"))], self.root, claude=False)
        with self.assertRaises(ValidationError):
            paper.upload(self.db, school.teacher, aid, st.user_id, [(f"p{i}.png", io.BytesIO(PNG)) for i in range(7)], self.root,
                         claude=False)
        sid = paper.upload(self.db, school.teacher, aid, st.user_id, [("p1.png", io.BytesIO(PNG)), ("p2.png", io.BytesIO(PNG))],
                           self.root, claude=False)
        self.assertEqual(len(paper.pages(self.root, school.inst, aid, st.user_id)), 2)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(paper.overview(tx, school.teacher, aid)["counts"]["to_check"], 1)
            view = paper.scan_view(tx, school.teacher, sid)
        form = {f"q{q['id']}": str(q["answer"]["index"]) for q in view["questions"]}
        att = paper.check_and_mark(self.db, self.engine, school.teacher, sid, form)
        with self.db.tenant(school.inst) as tx:
            row = tx.one("SELECT status, source FROM attempts WHERE id=%s", [att])
            self.assertEqual((row["status"], row["source"]), ("submitted", "paper"))
            got = tx.val("SELECT sum(ai_score) / sum(max_score) FROM responses WHERE attempt_id=%s", [att])
            self.assertEqual(float(got), 1.0)
            self.assertEqual(paper.overview(tx, school.teacher, aid)["counts"]["marked"], 1)
        with self.assertRaises(Conflict):
            paper.check_and_mark(self.db, self.engine, school.teacher, sid, form)
        other = school.student(1)
        attempts.submit(self.db, self.engine, other, self.answer_all(school, other, aid))
        with self.assertRaises(Conflict):
            paper.upload(self.db, school.teacher, aid, other.user_id, [("p.png", io.BytesIO(PNG))], self.root, claude=False)

    def test_claude_reads_handwriting_teacher_checks(self):
        """[PW-2][PW-3] With Claude, the photos are read in the background: Claude gets the pictures and the questions
        (with this student's own numbers) and returns what was written, not corrected; choices become the option
        picked. The teacher sees the reading next to the photos; answers Claude found unclear are flagged for a
        second look after marking unless the teacher ticks 'Read correctly'. Number answers are marked exactly."""
        from novaquest.ai.claude import ClaudeEngine
        from novaquest.ai.provider import ClaudeProvider
        from tests.test_ai import FakeServer, claude_reply

        school, aid, qs = self._setup()
        st = school.student(0)
        mcq = [q for q in qs if q["options"]]
        right = mcq[0]["answer"]["index"]
        wrong = (mcq[1]["answer"]["index"] + 1) % len(mcq[1]["options"])
        reply = {"answers": [{"no": 1, "choice": "ABCD"[right]}, {"no": 2, "choice": "ABCD"[wrong], "unclear": True}]}
        server = FakeServer(lambda b, h: claude_reply(json.dumps(reply)))
        try:
            eng = ClaudeEngine(ClaudeProvider("k", server.url, "f", "s", timeout_s=2, max_retries=0))
            sid = paper.upload(self.db, school.teacher, aid, st.user_id, [("p1.png", io.BytesIO(PNG))], self.root, claude=True)
            with self.db.tenant(school.inst) as tx:
                self.assertEqual(tx.val("SELECT status FROM paper_scans WHERE id=%s", [sid]), "reading")
                self.assertEqual(tx.val("SELECT count(*) FROM jobs WHERE kind=%s", [paper.JOB]), 1)
            paper.read(self.db, eng, school.inst, sid, self.root)
        finally:
            server.close()
        body = server.calls[0]["body"]
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertIn("exactly as written", body["system"][0]["text"])
        asked = json.loads(content[-1]["text"])
        self.assertEqual([q["no"] for q in asked["questions"]], [1, 2])
        with self.db.tenant(school.inst) as tx:
            view = paper.scan_view(tx, school.teacher, sid)
            self.assertEqual(view["scan"]["read_by"], "claude")
            self.assertEqual(view["questions"][0]["read"]["index"], right)
            self.assertTrue(view["questions"][1]["read"]["unclear"])
        form = {f"q{q['id']}": str(q["read"].get("index", "")) for q in view["questions"]}
        att = paper.check_and_mark(self.db, self.engine, school.teacher, sid, form)
        with self.db.tenant(school.inst) as tx:
            flagged = {r["question_id"]: r["flagged"] for r in tx.all("SELECT question_id, flagged FROM responses WHERE attempt_id=%s", [att])}
            self.assertTrue(flagged[mcq[1]["id"]], "unclear: a second look")
            self.assertFalse(flagged[mcq[0]["id"]])

    def test_numbers_privacy_and_pages(self):
        """[PW-1][PW-4][PR-3][PR-4][NF-A11Y] A number problem on paper is marked against the student's own numbers.
        Erasing a student deletes their photos; photos older than the school's period are deleted (marks stay). The
        Paper work page lists every student with where their work stands, and the check page shows the photos."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        aid = self.open_assignment(school, kind="numeric", topics=("11.8",), count=1)
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            q = questions.assignment_questions(tx, aid)[0]
        mine = maths.for_student(q, st.user_id)
        sid = paper.upload(self.db, school.teacher, aid, st.user_id, [("p.png", io.BytesIO(PNG))], self.root, claude=False)
        att = paper.check_and_mark(self.db, self.engine, school.teacher, sid,
                                   {f"q{q['id']}": f"{maths.fmt(mine['answer']['value'])} {mine['answer']['unit']}"})
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(float(tx.val("SELECT ai_score FROM responses WHERE attempt_id=%s", [att])), 1.0)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}/paper")[1]
            for text in ("Paper work", "Student 1", "Marked from paper", "Upload photos"):
                self.assertIn(text, html)
            self.assertIn(f"/teacher/assignments/{aid}/paper", t.get(f"/teacher/assignments/{aid}")[1])
        finally:
            live.stop()
        other = school.student(1)
        paper.upload(self.db, school.teacher, aid, other.user_id, [("p.png", io.BytesIO(PNG))], self.root, claude=False)
        with self.db.tenant(school.inst) as tx:
            privacy.erase_student(tx, school.admin, st.user_id, "Student 1", paper_root=self.root)
            self.assertEqual(tx.val("SELECT count(*) FROM paper_scans WHERE student_id=%s", [st.user_id]), 0)
        self.assertEqual(paper.pages(self.root, school.inst, aid, st.user_id), [])
        self.clock.advance(days=400)
        with self.db.tenant(school.inst) as tx:
            out = privacy.apply_retention(tx, paper_root=self.root)
            self.assertEqual(out["photos"], 1)
        self.assertEqual(paper.pages(self.root, school.inst, aid, other.user_id), [])
        with self.db.tenant(school.inst) as tx:
            self.assertIsNotNone(review.scorecard(tx, school.teacher, aid, st.user_id), "marks stay")
