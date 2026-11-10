"""1.25.0: a book printed in parts (NCERT Ganita Prakash Part 1 and Part 2) is one subject for the class: one tick
for the admin, one subject for students and teachers, with topics from either part."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from novaquest.core.errors import ValidationError
from novaquest.services import (assignments, books, exams, growth, institutions, learn, plans, questions, syllabus)
from tests.base import PASSWORD, NQTestCase
from tests.test_maths_books import maths_pdf


class BookParts(NQTestCase):
    def _parts(self, link_both: bool = True):
        """Part 1 (number found on its title) and Part 2 (chosen on upload) of one maths book, published."""
        school = self.make_school(n_students=2)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pdf = maths_pdf(tmp / "ch1.pdf")
        ids = []
        for title, part in (("Ganita Prakash Part-I", None), ("Ganita Prakash", 2)):
            with open(pdf, "rb") as fh:
                sid = books.create(self.db, school.admin, board="NCERT", subject="Mathematics", grade="8", title=title,
                                   files=[("ch1.pdf", fh)], storage=tmp / "store", part=part)
            books.read_book(self.db, self.engine, school.inst, sid, tmp / "store")
            ids.append(sid)
            self.clock.advance(minutes=5)
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, ids[0])
            if link_both:
                books.publish(tx, school.admin, ids[1])
                institutions.set_class_subjects(tx, school.admin, school.class_id, ids)
            else:
                institutions.set_class_subjects(tx, school.admin, school.class_id, [ids[0]])
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id,
                                        syllabus_id=ids[0])
            topics = {sid: [r["id"] for r in tx.all("""SELECT id FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic'
                                                       ORDER BY ord""", [sid])] for sid in ids}
        return school, ids, topics

    def test_upload_and_admin_setup(self):
        """[BP-1] An admin uploads each part as its own book with the same board, subject and grade and says which part
        it is (or NovaQuest reads 'Part-I' / 'Part-II' from the title or cover). The parts show as one subject when
        setting up a class ('Mathematics · NCERT Grade 8 (Part 1 + 2)', one tick links both), mapping a teacher to the
        subject maps every part, and a part published later joins the classes and teachers that have the other part.
        A part number outside 1-9 is refused."""
        self.assertEqual(books.detect_part("Maths", "Textbook of Mathematics for Grade 8\nPart-II\n...as in Part-I of"), 2)
        self.assertEqual(books.detect_part("Ganita Prakash Part 1"), 1)
        self.assertIsNone(books.detect_part("Science", "No part of this publication may be reproduced"))
        school, (p1, p2), _ = self._parts(link_both=False)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual([syllabus.get_syllabus(tx, i)["part"] for i in (p1, p2)], [1, 2])
            self.assertIn("Part 2", syllabus.get_syllabus(tx, p2)["label"])
            with self.assertRaises(ValidationError):
                books.set_part(tx, school.admin, p2, "12")
            books.publish(tx, school.admin, p2)  # published after the class got Part 1: it joins the class and teacher
            self.assertEqual(syllabus.class_subjects(tx, school.class_id)[-1]["ids"], [p1, p2])
            mine = {r["syllabus_id"] for r in tx.all("SELECT syllabus_id FROM teaching_assignments WHERE teacher_id=%s",
                                                     [school.teacher.user_id])}
            self.assertTrue({p1, p2} <= mine)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("admin@sun.test", PASSWORD)
            page = c.get("/admin")[1]
            self.assertIn(f'value="{p1},{p2}"', page)
            self.assertIn("(Part 1 + 2)", page)
            c.post("/admin/subjects", {"class_id": str(school.class_id), "books[]": f"{p1},{p2}"})
            with self.db.tenant(school.inst) as tx:
                self.assertEqual(sorted(b["id"] for b in syllabus.class_syllabi(tx, school.class_id)), sorted([p1, p2]))
            c.post(f"/admin/books/{p2}/part", {"part": ""})
            with self.db.tenant(school.inst) as tx:
                self.assertIsNone(syllabus.get_syllabus(tx, p2)["part"])
            self.assertIn('name="part"', c.get("/admin/books")[1])
        finally:
            live.stop()

    def test_one_subject_for_students(self):
        """[BP-2][UX-1] Students see one Mathematics subject: Learn lists Part 1's chapters then Part 2's
        ('Part 2 · 1. …'), and tasks from either part share one subject tag and filter."""
        school, (p1, p2), topics = self._parts()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            subs = learn.student_books(tx, st)
            maths = [s for s in subs if p1 in s["ids"]]
            self.assertEqual(len(maths), 1)
            self.assertEqual(maths[0]["ids"], [p1, p2])
            self.assertNotIn("Part", maths[0]["label"])
            chapters = learn.topics_for_student(tx, st, p2)
            self.assertEqual([c["part"] for c in chapters], [1, 2])
            self.assertEqual({t["syllabus_id"] for c in chapters for t in c["topics"]}, {p1, p2})
        a1 = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                           topic_ids=topics[p1][:1], kind="quiz", difficulty="medium", count=2, title="P1")
        a2 = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                           topic_ids=topics[p2][:1], kind="quiz", difficulty="medium", count=2, title="P2")
        from datetime import timedelta

        from novaquest.core import clock
        with self.db.tenant(school.inst) as tx:
            for aid in (a1, a2):
                for q in questions.assignment_questions(tx, aid):
                    questions.approve(tx, school.teacher, q["id"])
                assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                     expires_at=clock.now() + timedelta(hours=5), targets=[("class", school.class_id)])
            mine = [a for a in assignments.for_student(tx, st.user_id) if a["id"] in (a1, a2)]
        self.assertEqual({a["subject_id"] for a in mine}, {p1})
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login_student("SUN", "8B", "1", school.students[0]["pin"])
            page = c.get(f"/student/learn?book={p2}")[1]
            self.assertIn("Part 2 · ", page)
            self.assertIn("Part 1 · ", page)
            tasks = c.get(f"/student/tasks?book={p2}")[1]
            self.assertIn("P1", tasks)
            self.assertIn("P2", tasks)
        finally:
            live.stop()

    def test_one_subject_for_teachers(self):
        """[BP-3] Teachers pick the subject once and choose topics from either part (topics marked 'P2·1.1'):
        assignments and exam papers can mix the parts and each question belongs to its own part; the term plan,
        start-of-term check and growth report cover the whole book. Topics from another book can't be mixed in."""
        school, (p1, p2), topics = self._parts()
        mixed = [topics[p1][0], topics[p2][0]]
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id, topic_ids=mixed,
                                            kind="quiz", difficulty="medium", count=4, title="Both parts")
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
            by_topic = {r["id"]: r["syllabus_id"] for r in tx.all("SELECT id, syllabus_id FROM questions WHERE id = ANY(%s)",
                                                                  [[q["id"] for q in qs]])}
            self.assertEqual({by_topic[q["id"]] for q in qs}, {p1, p2})
            self.assertTrue(any(q["topic_code"].startswith("P2·") for q in qs))
            self.assertEqual(tx.val("SELECT syllabus_id FROM assignments WHERE id=%s", [aid]), p1)
        with self.assertRaises(ValidationError):
            questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                          topic_ids=[topics[p1][0], school.topics["8.3"]], kind="quiz", difficulty="medium",
                                          count=2, title="Mixed books")
        made = exams.create(self.db, self.engine, school.teacher, class_id=school.class_id, syllabus_id=p1, title="Test",
                            topic_ids=mixed, pattern=[{"name": "A", "kind": "mcq", "count": 2, "marks": 1}],
                            difficulty="mixed", duration_min=30)
        with self.db.tenant(school.inst) as tx:
            paper = exams.view(tx, school.teacher, made["id"])
            text = str(paper)
            self.assertIn("Part 2 · ", text)
            plan = plans.view(tx, school.teacher, school.class_id, p2)
            self.assertEqual([c["part"] for c in plan["chapters"]], [1, 2])
            spread = growth._spread(tx, p1)
            self.assertTrue(set(spread) & set(topics[p2]))
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            page = c.get(f"/teacher/assignments/new?for={school.class_id}:{p2}")[1]
            self.assertIn("P2·", page)
            self.assertIn("Part 2 · ", page)
            self.assertIn("Part 2 · ", c.get(f"/teacher/plan?for={school.class_id}:{p1}")[1])
            self.assertIn("Part 2 · ", c.get(f"/teacher/syllabus?for={school.class_id}:{p1}")[1])
            grid = c.get(f"/teacher/classes/{school.class_id}/mastery?book={p2}")[1]
            self.assertIn("P1·1.1", grid)
            self.assertIn("Part 2 · 1. ", grid)
        finally:
            live.stop()
