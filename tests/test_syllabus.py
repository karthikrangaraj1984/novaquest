"""Syllabus board and grounding (SY-1..SY-4) plus PDF ingestion."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from novaquest.core.errors import Conflict, ValidationError
from novaquest.services import ingest, learn, search, syllabus
from tests.base import ROOT, NQTestCase


class SyllabusBoard(NQTestCase):
    def test_tree_subject_chapter_topic_outcome(self):
        """[SY-1] Syllabus shown as a tree: subject → chapter → topic → learning outcome, for the whole subject."""
        with self.db.anonymous() as tx:
            sid = syllabus.current_syllabus_id(tx)
            tree = syllabus.get_tree(tx, sid)
        self.assertEqual(len(tree), 18)
        self.assertEqual(sum(len(c["topics"]) for c in tree), 125)
        self.assertEqual(tree[7]["title"], "Cell — Structure and Functions")
        self.assertTrue(all(t["outcomes"] for c in tree for t in c["topics"]))
        self.assertEqual(tree[7]["topics"][4]["code"], "8.5")

    def test_topics_have_cited_source_and_coverage_marker(self):
        """[SY-2] Each topic shows its textbook source with page citations, and whether the class has practised it."""
        school = self.make_school()
        with self.db.anonymous() as tx:
            n = tx.val("SELECT count(*) FROM syllabus_nodes t WHERE kind='topic' AND NOT EXISTS (SELECT 1 FROM source_passages p WHERE p.node_id=t.id)")
            passages = syllabus.topic_passages(tx, school.topics["8.5"])
        self.assertEqual(n, 0, "every topic has textbook passages")
        self.assertRegex(passages[0]["ref"], r"NCERT Science Class VIII, Ch 8 §8\.5, p\. 9\d")
        self.assertIn("membrane", " ".join(p["body"] for p in passages).lower())
        self.open_assignment(school, topics=("8.5",), count=3)
        with self.db.tenant(school.inst) as tx:
            assigned = {t for r in tx.all("SELECT topic_ids FROM assignments WHERE class_id=%s AND state<>'draft'", [school.class_id]) for t in r["topic_ids"]}
        self.assertIn(school.topics["8.5"], assigned)
        self.assertNotIn(school.topics["8.1"], assigned)

    def test_teacher_notes_become_citable_source_for_own_school_only(self):
        """[SY-3] Teacher uploads notes to a topic; generated questions and answers can cite them; other schools can't see them."""
        school = self.make_school()
        other = self.make_school("OTH")
        notes = ("Mitochondria are called the powerhouse of the cell because they release energy from food. "
                 "Ribosomes make proteins. Our class saw onion peel cells under the school microscope on Monday.")
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                syllabus.add_teacher_notes(tx, school.topics["8.5"], "too short", title="x", actor_id=school.teacher.user_id)
            n = syllabus.add_teacher_notes(tx, school.topics["8.5"], notes, title="Lab notes", actor_id=school.teacher.user_id)
            self.assertGreaterEqual(n, 1)
            res = search.search(tx, school.syllabus_id, "Why are mitochondria called the powerhouse?")
            self.assertTrue(res.on_syllabus)
            self.assertTrue(res.hits[0].ref.startswith("Teacher notes: Lab notes"))
        reply = learn.ask(self.db, self.engine, school.student(0), "Why are mitochondria called the powerhouse?", school.topics["8.5"])
        self.assertIn("Teacher notes", " ".join(reply.citations))
        with self.db.tenant(other.inst) as tx:
            self.assertFalse(any(h.ref.startswith("Teacher notes") for h in search.search(tx, other.syllabus_id, "mitochondria powerhouse").hits))
        with tempfile.TemporaryDirectory() as d:
            text = syllabus.extract_upload_text("notes.txt", notes.encode(), Path(d))
            self.assertIn("powerhouse", text)
            with self.assertRaises(ValidationError):
                syllabus.extract_upload_text("notes.exe", b"x", Path(d))

    def test_new_version_does_not_break_existing_assignments(self):
        """[SY-4] NovaQuest admin loads a new syllabus version; existing assignments keep working."""
        school = self.make_school()
        aid = self.open_assignment(school, topics=("8.5",), count=3)
        spec = json.loads((ROOT / "data/syllabus/ncert_g8_science.json").read_text())
        with self.assertRaises(Conflict):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "m.json"
                p.write_text(json.dumps(spec))
                syllabus.load_syllabus(self.db, p, None, actor_id=None)
        spec["version"] = "2026-27-test"
        spec["chapters"][7]["topics"][4]["title"] = "Parts of the Cell (revised)"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps(spec))
            rep = syllabus.load_syllabus(self.db, p, None, actor_id=None)
        try:
            with self.db.tenant(school.inst) as tx:
                self.assertEqual(syllabus.current_syllabus_id(tx), rep.syllabus_id)
                self.assertEqual(len(syllabus.list_versions(tx)), 2)
                from novaquest.services import attempts, questions
                self.assertTrue(questions.assignment_questions(tx, aid))
            att = self.answer_all(school, school.student(0), aid)
            self.assertEqual(attempts.submit(self.db, self.engine, school.student(0), att)["status"], "submitted")
        finally:
            with self.db.platform() as tx:  # restore the real syllabus as current for other tests
                tx.exec("DELETE FROM lessons WHERE node_id IN (SELECT id FROM syllabus_nodes WHERE syllabus_id=%s)", [rep.syllabus_id])
                tx.exec("DELETE FROM misconceptions WHERE syllabus_id=%s", [rep.syllabus_id])
                tx.exec("DELETE FROM syllabus_nodes WHERE syllabus_id=%s AND kind='outcome'", [rep.syllabus_id])
                tx.exec("DELETE FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic'", [rep.syllabus_id])
                tx.exec("DELETE FROM syllabus_nodes WHERE syllabus_id=%s", [rep.syllabus_id])
                tx.exec("DELETE FROM syllabi WHERE id=%s", [rep.syllabus_id])
                tx.exec("UPDATE syllabi SET is_current=true WHERE id=%s", [school.syllabus_id])
            syllabus.get_tree.cache_clear()


class Ingestion(NQTestCase):
    def test_pdf_split_into_sections_with_pages(self):
        """[SY-4] The textbook PDF is split into cited passages for all 125 topics (re-runnable for a new edition)."""
        pdf = ROOT / "data/source/ncert_science_class8_2018.pdf"
        spec = json.loads((ROOT / "data/syllabus/ncert_g8_science.json").read_text())
        pages = ingest.extract_pages(pdf)
        self.assertEqual(len(pages), 265)
        total, covered = 0, set()
        for ch in spec["chapters"]:
            ps = ingest.build_passages(pages, ch, spec["pdf_page_offset"])
            total += len(ps)
            covered |= {p.section for p in ps}
            self.assertTrue(all(ch["start_page"] <= p.page <= ch["end_page"] for p in ps))
        self.assertEqual(covered, {t["sec"] for c in spec["chapters"] for t in c["topics"]})
        self.assertGreater(total, 400)

    def test_pypdf_fallback_repairs_split_words(self):
        """[SY-4] Without poppler, the pure-Python reader is used and split words are repaired."""
        fixed = ingest.repair_split_words(["You have lear nt that each organ is made of cells. You learnt this."])
        self.assertIn("learnt that", fixed[0])
