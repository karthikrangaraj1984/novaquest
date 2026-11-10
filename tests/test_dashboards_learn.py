"""Dashboards and exports (DB-1..DB-5) and Learn mode (LN-1..LN-9)."""
from __future__ import annotations

import csv
import io

import pypdf

from novaquest.core.errors import Forbidden, NotFound, ValidationError
from novaquest.services import attempts, dashboards, learn, mastery, pdfs, review, search, syllabus
from tests.base import NQTestCase


class Dashboards(NQTestCase):
    def _class_with_results(self, n: int = 4):
        school = self.make_school(n_students=n)
        aid = self.open_assignment(school, kind="mission", count=6)
        for i in range(n - 1):
            att = self.answer_all(school, school.student(i), aid, good=(i % 2 == 0))
            attempts.submit(self.db, self.engine, school.student(i), att)
        return school, aid

    def test_teacher_assignment_results_and_drilldown(self):
        """[DB-1] Teacher sees per-assignment results, completion rate and can open any student's scorecard."""
        school, aid = self._class_with_results()
        with self.db.tenant(school.inst) as tx:
            res = dashboards.assignment_results(tx, school.teacher, aid)
            self.assertEqual(res["summary"]["completion"], 75)
            self.assertEqual(res["summary"]["submitted"], 3)
            self.assertEqual(len(res["students"]), 4)
            done = [s for s in res["students"] if s["status"] == "submitted"]
            card = review.scorecard(tx, school.teacher, aid, done[0]["id"])
            self.assertEqual(len(card["rows"]), 6)
            self.assertEqual([s["name"] for s in res["summary"]["missing"]], ["Student 4"])

    def test_class_mastery_weakest_outcomes_and_misconceptions(self):
        """[DB-2] Class mastery by learning outcome, with the weakest three outcomes and most common misconceptions highlighted."""
        school, aid = self._class_with_results()
        with self.db.tenant(school.inst) as tx:
            data = dashboards.class_mastery(tx, school.class_id)
        active = [r for r in data["rows"] if r["avg_score"] is not None]
        self.assertTrue(active)
        self.assertTrue(all(r["outcome"] for r in data["rows"]))
        self.assertLessEqual(len(data["weakest"]), 3)
        self.assertEqual([w["topic_id"] for w in data["weakest"]],
                         [r["topic_id"] for r in sorted(active, key=lambda r: float(r["avg_score"]))][:3])
        self.assertTrue(data["misconceptions"], "wrong answers on misconception questions are tagged and rolled up")
        self.assertGreater(sum(data["levels"].values()), 0)

    def test_leadership_compares_classes_trend_and_engagement(self):
        """[DB-3] Principal sees all classes on one screen: class/teacher comparison, trend over time, who isn't completing."""
        school, aid = self._class_with_results(6)  # 5 hand in: enough students to show a class figure
        from novaquest.services import institutions
        with self.db.tenant(school.inst) as tx:
            institutions.create_class(tx, school.admin, grade="8", section="A", academic_year="2026-27")
        self.clock.advance(days=2)
        with self.db.tenant(school.inst) as tx:
            data = dashboards.leadership(tx, school.leader)
            with self.assertRaises(Forbidden):
                dashboards.leadership(tx, school.teacher)
        self.assertEqual(sorted(c["label"] for c in data["classes"]), ["8A", "8B"])
        b = next(c for c in data["classes"] if c["label"] == "8B")
        self.assertIn("Mrs Rao", b["teachers"])
        self.assertIsNotNone(b["understanding"])
        self.assertTrue(data["trend"])
        self.assertEqual([s["name"] for s in data["not_completing"]], ["Student 6"])
        self.assertEqual(data["kpis"]["students"], 6)
        self.assertEqual((b["on_time"], b["closed"]), (83, 1), "5 of the 6 students set the task handed in on time")

    def test_exports_match_screen(self):
        """[DB-4] Dashboards export to CSV and PDF with the same figures as the screen."""
        school, aid = self._class_with_results(6)  # 5 hand in: enough students to show a class figure
        self.clock.advance(days=2)
        with self.db.tenant(school.inst) as tx:
            data = dashboards.leadership(tx, school.leader)
            cls = dashboards.class_mastery(tx, school.class_id)
        rows = list(csv.reader(io.StringIO(dashboards.leadership_csv(data))))
        b = next(c for c in data["classes"] if c["label"] == "8B")
        line = next(r for r in rows if r and r[0] == "8B")
        self.assertEqual(line[3], str(b["understanding"]))
        self.assertEqual(line[4], str(b["active_pct"]))
        crow = list(csv.reader(io.StringIO(dashboards.class_csv(cls, "8B"))))
        self.assertEqual(len(crow) - 1, len(cls["rows"]))
        pdf = pdfs.table_report("8B", "x", ["Class", "Understanding %"], [[c["label"], c["understanding"]] for c in data["classes"]])
        self.assertIn(str(b["understanding"]), pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text())

    def test_ai_concepts_tracked_as_own_section(self):
        """[DB-5] AI concepts are tracked alongside syllabus concepts and shown as their own section."""
        school, aid = self._class_with_results()
        with self.db.tenant(school.inst) as tx:
            skills = dashboards.ai_skills(tx, [school.class_id])
            card = review.scorecard(tx, school.teacher, aid, school.students[0]["student_id"])
        self.assertTrue(skills)
        self.assertEqual(skills[0]["concept"], "Checking AI claims")
        self.assertTrue(any(c["ai"] for c in card["concepts"]) and any(not c["ai"] for c in card["concepts"]))
        self.assertIsNotNone(card["ai"])


class LearnMode(NQTestCase):
    def test_guided_lesson_cites_source_and_checks_adapt(self):
        """[LN-1] Guided lesson: explanation with its source, a worked example, quick checks that adapt when wrong."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            les = learn.lesson(tx, st, school.topics["8.5"])
            self.assertIn("cell membrane", les["explain"])
            self.assertTrue(les["example"])
            self.assertRegex(les["refs"][0], r"Ch 8 §8\.5")
            self.assertEqual(len(les["checks"]), 3)
            self.assertNotIn("answer", les["checks"][0])
            wrong = learn.answer_check(tx, st, school.topics["8.5"], 0, 0)
            self.assertFalse(wrong["correct"])
            self.assertIn("school", wrong["explain_again"])  # explained a different way
            right = learn.answer_check(tx, st, school.topics["8.5"], 0, 1)
            self.assertTrue(right["correct"])
            other = learn.lesson(tx, st, school.topics["11.8"])
            self.assertTrue(other["from_textbook"])
            self.assertTrue(other["checks"])
            self.assertRegex(other["refs"][0], r"Ch 11 §11\.8")

    def test_ask_tutor_grounded_cited_and_declines_off_syllabus(self):
        """[LN-2] Ask the tutor: answers are grounded with a chapter reference; off-syllabus questions are politely declined."""
        school = self.make_school()
        st = school.student(0)
        r = learn.ask(self.db, self.engine, st, "What does the nucleus do?", school.topics["8.5"])
        self.assertFalse(r.declined)
        self.assertIn("control", r.text.lower())
        self.assertRegex(r.citations[0], r"Ch 8 §8\.5")
        for q in ("Who won the cricket world cup?", "What is the capital of France?", "Tell me a joke about movies"):
            self.assertTrue(learn.ask(self.db, self.engine, st, q).declined, q)
        unkind = learn.ask(self.db, self.engine, st, "you are stupid")
        self.assertIn("kind", unkind.text)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM tutor_messages WHERE student_id=%s", [st.user_id]), 10)

    def test_ask_tutor_understands_casual_questions(self):
        """[LN-2] Students' casual phrasing ("whats a bacteria", "tell me about plastics") is answered from the
        textbook, while off-syllabus questions asked the same way are still declined; with no textbook loaded,
        NOVA says so instead of calling the question off-syllabus."""
        from unittest import mock

        school = self.make_school()
        st = school.student(0)
        for q, ch in (("whats a bacteria", "2"), ("what's a virus", "2"), ("tell me about plastics", "3"),
                      ("explain fermentation", "2"), ("wat is a cell", "8"), ("Whats the use of a fire extinguisher?", "6")):
            r = learn.ask(self.db, self.engine, st, q)
            self.assertFalse(r.declined, q)
            self.assertRegex(r.citations[0], rf"Ch {ch} §", q)
        for q in ("whats the capital of France", "tell me a joke about movies", "whats the price of an iphone"):
            self.assertTrue(learn.ask(self.db, self.engine, st, q).declined, q)
        empty = search.SearchResult([], 0.0, False)
        with mock.patch.object(search, "search", return_value=empty), mock.patch.object(syllabus, "passage_count", return_value=0):
            r = learn.ask(self.db, self.engine, st, "what is a virus")
        self.assertTrue(r.declined)
        self.assertIn("textbook hasn't been added", r.text)
        self.assertNotIn("outside", r.text)

    def test_practice_mission_completes(self):
        """[LN-3] Interactive practice: a game-style mission per topic that the student can complete."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            m = learn.practice(tx, st, school.topics["8.5"])
            self.assertGreaterEqual(len(m["questions"]), 3)
            self.assertTrue(all(q["scenario"].startswith("Mission stage") for q in m["questions"]))
            res = learn.practice_submit(tx, st, school.topics["8.5"], {i: 0 for i in range(len(m["questions"]))})
        self.assertEqual(len(res["results"]), len(m["questions"]))
        self.assertGreaterEqual(res["level"], 1)

    def test_understanding_check_sets_mastery(self):
        """[LN-4] End-of-topic understanding check (explain back + apply) sets the topic's mastery level."""
        school = self.make_school()
        st = school.student(0)
        weak = learn.understanding_check(self.db, self.engine, st, school.topics["8.5"], "cells are small", "not sure about it")
        self.assertLess(weak["score"], 0.75)
        good = learn.understanding_check(self.db, self.engine, st, school.topics["8.5"],
                                         "The nucleus controls the cell and the cytoplasm holds organelles, inside the cell membrane.",
                                         "For example the membrane lets water in but the cell wall only gives support.")
        self.assertGreaterEqual(good["score"], 0.75)
        self.assertEqual(good["level"], 3)
        with self.assertRaises(ValidationError):
            learn.understanding_check(self.db, self.engine, st, school.topics["8.5"], "", "")

    def test_mastery_levels_for_student_and_teacher(self):
        """[LN-5] Mastery (not started, learning, practised, mastered) shown for the student and per student for the teacher."""
        school = self.make_school(n_students=2)
        st = school.student(0)
        t = school.topics["8.6"]
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(mastery.for_student(tx, st.user_id).get(t), None)
            learn.lesson(tx, st, t)
            self.assertEqual(mastery.for_student(tx, st.user_id)[t]["level"], 1)
            learn.answer_check(tx, st, t, 0, 2)
            self.assertEqual(mastery.for_student(tx, st.user_id)[t]["level"], 2)
        learn.understanding_check(self.db, self.engine, st, t, "A plant cell has a cell wall, plastids and a large vacuole; an animal cell does not.",
                                  "For example a leaf cell has chloroplasts but a cheek cell (an animal cell) has no cell wall.")
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(mastery.for_student(tx, st.user_id)[t]["level"], 3)
            rows = {r["student_id"]: r["level"] for r in mastery.for_class(tx, school.class_id, [t])}
            topics = learn.topics_for_student(tx, st)
        self.assertEqual(rows[st.user_id], 3)
        self.assertIsNone(rows[school.students[1]["student_id"]])
        self.assertEqual(next(x for c in topics for x in c["topics"] if x["id"] == t)["level"], 3)

    def test_learn_assignment_notifies_teacher_when_done(self):
        """[LN-6] Teacher assigns a topic to learn before class; finishing the lesson check completes it and notifies the teacher."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="learn", topics=("8.6",), count=1)
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            attempts.start_or_resume(tx, st, aid)
        learn.understanding_check(self.db, self.engine, st, school.topics["8.6"],
                                  "Plant cells have a cell wall, plastids and a large vacuole.", "For example leaf cells have chloroplasts.")
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT status FROM attempts WHERE assignment_id=%s", [aid]), "submitted")
            body = tx.val("SELECT body FROM notifications WHERE user_id=%s AND kind='completion'", [school.teacher.user_id])
        self.assertIn("1 of 6", body)

    def test_teacher_locks_topics_and_reads_conversations(self):
        """[LN-7] Teacher chooses which topics are open for self-study (locked ones are hidden) and reads tutor conversations."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            learn.set_topic_open(tx, school.teacher, school.class_id, school.topics["10.6"], False)
            visible = {t["id"] for c in learn.topics_for_student(tx, st) for t in c["topics"]}
            self.assertNotIn(school.topics["10.6"], visible)
            with self.assertRaises(NotFound):
                learn.lesson(tx, st, school.topics["10.6"])
            with self.assertRaises(Forbidden):
                learn.set_topic_open(tx, school.leader, school.class_id, school.topics["10.6"], True)
        learn.ask(self.db, self.engine, st, "What does the cell membrane do?", school.topics["8.5"])
        with self.db.tenant(school.inst) as tx:
            log = learn.tutor_log(tx, school.teacher, st.user_id)
            self.assertEqual([m["role"] for m in log], ["tutor", "student"])
            learn.set_topic_open(tx, school.teacher, school.class_id, school.topics["10.6"], True)
            self.assertTrue(learn.lesson(tx, st, school.topics["10.6"]))

    def test_adaptive_next_step(self):
        """[LN-8] After each understanding check: revise a weak concept (explained differently) or move on."""
        school = self.make_school()
        st = school.student(0)
        weak = learn.understanding_check(self.db, self.engine, st, school.topics["8.3"], "cells are cells", "I do not know this one")
        self.assertEqual(weak["next"]["action"], "revise")
        good = learn.understanding_check(self.db, self.engine, st, school.topics["8.3"],
                                         "Unicellular organisms like amoeba have one cell, multicellular have many; cells vary in shape and size.",
                                         "For example a nerve cell is long to carry messages.")
        self.assertEqual(good["next"]["action"], "next")
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(good["next"]["topic_id"], school.topics["8.4"])
        last = learn.understanding_check(self.db, self.engine, st, school.topics["8.6"],
                                         "A plant cell has a cell wall, plastids and a large vacuole; an animal cell does not.",
                                         "For example leaf cells have chloroplasts.")
        self.assertEqual(last["next"]["action"], "teach")

    def test_revision_plan_weakest_first(self):
        """[LN-9] Revision plan before exams lists the student's weakest topics in priority order with lesson links."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, st.user_id, school.topics["8.1"], "check", 0.9)
            mastery.record(tx, st.user_id, school.topics["8.2"], "check", 0.1)
            mastery.record(tx, st.user_id, school.topics["11.8"], "check", 0.4)
            mastery.record(tx, st.user_id, school.topics["8.6"], "understanding", 0.95)
            plan = learn.revision_plan(tx, st)
        self.assertEqual([p["code"] for p in plan], ["8.2", "11.8", "8.1"])
        self.assertNotIn("8.6", [p["code"] for p in plan])
