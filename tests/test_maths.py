"""1.20.0: number problems for maths and science: reading answers, units, tolerance, each student's own numbers,
step hints and worked solutions."""
from __future__ import annotations

import unittest

from novaquest.core.errors import ValidationError
from novaquest.services import attempts, maths, questions, review
from tests.base import PASSWORD, NQTestCase


class ReadingAnswers(unittest.TestCase):
    def test_answers_read_the_way_students_write_them(self):
        """[MA-1] Numbers are read as students write them (24, 24 cm², 24cm^2, 24 sq cm, 1,200, 1,20,000, 3/4, 1 1/2,
        2.5e3, 2.5 × 10^3, −5, 45%, ₹ 250, 'A = 24 cm2', 'about 24'), with units recognised."""
        cases = {"24": (24, None), "24 cm²": (24, "cm²"), "24cm^2": (24, "cm²"), "24 sq cm": (24, "cm²"), "1,200": (1200, None),
                 "1,20,000": (120000, None), "3/4": (0.75, None), "1 1/2": (1.5, None), "2.5e3": (2500, None),
                 "2.5 × 10^3": (2500, None), "−5": (-5, None), "45%": (45, "%"), "₹ 250": (250, "₹"), "Rs. 250": (250, "₹"),
                 "A = 24 cm2": (24, "cm²"), "about 24": (24, None), "7 hrs": (7, "h"), "4 m/s^2": (4, "m/s²")}
        for text, (v, u) in cases.items():
            value, unit, _ = maths.parse(text)
            self.assertAlmostEqual(value, v, msg=text)
            self.assertEqual(unit, u, text)
        self.assertIsNone(maths.parse("I don't know")[0])

    def test_marking_units_tolerance_and_common_slips(self):
        """[MA-1][MA-2] Units are converted (0.0024 m² is 24 cm²); the right number with a missing or wrong unit gets
        half marks with 'check the unit'; close but outside the tolerance gets half and 'check your rounding';
        off by 10/100/1000 says 'a unit slip?'; the right size with the wrong sign says 'check the sign'. No AI."""
        q = {"answer": {"value": 24, "unit": "cm²"}}
        expect = {"24 cm²": 1.0, "0.0024 m²": 1.0, "24": 0.5, "24 m": 0.5, "2400 cm²": 0.0, "-24 cm²": 0.0, "24.6 cm²": 0.5,
                  "30 cm²": 0.0, "": 0.0}
        for text, score in expect.items():
            self.assertEqual(maths.mark(q, {"text": text}).score, score, text)
        self.assertIn("unit", maths.mark(q, {"text": "24"}).rationale)
        self.assertIn("power of ten", maths.mark(q, {"text": "2400 cm²"}).rationale)
        self.assertIn("sign", maths.mark(q, {"text": "-24 cm²"}).rationale)
        self.assertIn("rounding", maths.mark(q, {"text": "24.6 cm²"}).rationale)
        self.assertEqual(maths.mark({"answer": {"value": 3.14, "abs": 0.01}}, {"text": "3.142"}).score, 1.0)
        self.assertLess(maths.mark({"answer": {"value": 1.0}}, {"text": "hmm"}).confidence, 0.7, "no number: teacher checks")

    def test_templates_give_each_student_their_own_numbers(self):
        """[MA-3] A question with number ranges and a formula gives each student their own numbers, the same each time
        they open it; its hints and worked solution use those numbers; every ready-made problem marks its own
        worked answer right for hundreds of students; formulas can only do arithmetic; hints that give the answer
        away are refused."""
        prompt, key = maths.build_key(prompt="A rectangle is {l} cm by {b} cm. Find its area.", formula="l*b",
                                      vars_text="l = 3..12, b = 2..9", unit="sq cm", steps=["Area = length × breadth", "= {l} × {b}"],
                                      worked="{l} × {b} = {answer} cm²")
        self.assertEqual(key["unit"], "cm²")
        q = {"id": 7, "kind": "numeric", "prompt": prompt, "answer": key}
        a, b = maths.for_student(q, 1), maths.for_student(q, 2)
        self.assertEqual(a, maths.for_student(q, 1), "the same numbers every time")
        self.assertNotEqual(a["prompt"], b["prompt"])
        inst = maths.instance(key, maths.seed_for(7, 1))
        self.assertIn(maths.fmt(inst["values"]["l"]), a["answer"]["steps"][1])
        self.assertTrue(a["answer"]["worked"].endswith(f"= {maths.fmt(a['answer']['value'], 2)} cm²"))
        self.assertEqual(maths.mark(a, {"text": f"{maths.fmt(a['answer']['value'])} cm2"}).score, 1.0)
        for t in maths.LIBRARY:
            k = maths._key_from(dict(t))
            for seed in range(150):
                i = maths.instance(k, seed)
                text = f"{maths.fmt(i['value'], k['decimals'])} {k['unit']}"
                self.assertEqual(maths.mark({"answer": {**k, "value": i["value"]}}, {"text": text}).score, 1.0, (t["prompt"], text))
                self.assertNotIn("{", i["prompt"] + "".join(i["steps"]) + i["worked"])
        with self.assertRaises(ValueError):
            maths.evaluate("__import__('os').system('x')", {})
        with self.assertRaises(ValidationError):
            maths.build_key(prompt="Double {x}.", formula="x*2", vars_text="x = 1..5", steps=["It is {answer}."], worked="")
        with self.assertRaises(ValidationError):
            maths.build_key(prompt="Area {l}.", formula="l*b", vars_text="l = 1..5", steps=[], worked="")
        with self.assertRaises(ValidationError):
            maths.build_key(prompt="What is 6 × 4?", value="24", steps=["6 × 4 = 24"], worked="")


class NumberProblems(NQTestCase):
    def test_number_assignment_end_to_end(self):
        """[MA-1][MA-3][MA-4][AS-1] A teacher sets 'Number problems' on a topic (here Pressure): NovaQuest drafts
        them (from its ready-made problems when Claude isn't there). Each student sees their own numbers; 'Need a
        hint?' gives the method one step at a time, never the answer, without calling Claude; marking is exact;
        the scorecard shows the worked solution with the student's numbers. A teacher can also add their own
        number question with number ranges."""
        school = self.make_school(n_students=2)
        aid = self.open_assignment(school, kind="numeric", topics=("11.8",), count=2)
        s0, s1 = school.student(0), school.student(1)
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
            self.assertTrue(qs and all(q["kind"] == "numeric" for q in qs))
            self.assertIn("pressure", qs[0]["prompt"].lower())
            v0, v1 = attempts.view(tx, s0, attempts.start_or_resume(tx, s0, aid)["id"]), None
            att1 = attempts.start_or_resume(tx, s1, aid)
            v1 = attempts.view(tx, s1, att1["id"])
            self.assertNotEqual(v0["questions"][0]["prompt"], v1["questions"][0]["prompt"], "each student's own numbers")
        att0 = v0["attempt"]["id"]
        qid = qs[0]["id"]
        h1 = attempts.hint(self.db, self.engine, s0, att0, qid)
        h2 = attempts.hint(self.db, self.engine, s0, att0, qid)
        mine = maths.for_student(qs[0], s0.user_id)
        self.assertIn("Step 1", h1.text)
        self.assertIn("Step 2", h2.text)
        self.assertNotIn(maths.fmt(mine["answer"]["value"]), h1.text + h2.text)
        with self.db.tenant(school.inst) as tx:
            for q in qs:
                m = maths.for_student(q, s0.user_id)
                attempts.save_answer(tx, s0, att0, q["id"], {"text": f"{maths.fmt(m['answer']['value'])} {m['answer']['unit']}"})
            wrong = maths.for_student(qs[0], s1.user_id)
            attempts.save_answer(tx, s1, att1["id"], qs[0]["id"], {"text": f"{maths.fmt(wrong['answer']['value'])}"})
        attempts.submit(self.db, self.engine, s0, att0)
        attempts.submit(self.db, self.engine, s1, att1["id"])
        with self.db.tenant(school.inst) as tx:
            got = {r["student_id"]: r for r in tx.all("""SELECT t.student_id, sum(r.ai_score) AS s, sum(r.max_score) AS m
                FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE t.assignment_id=%s GROUP BY t.student_id""", [aid])}
            self.assertEqual(float(got[s0.user_id]["s"]), float(got[s0.user_id]["m"]))
            self.assertEqual(float(got[s1.user_id]["s"]), 0.5, "right number, no unit: half")
            review.release(tx, school.teacher, aid)
            card = review.scorecard(tx, s0, aid)
            row = card["rows"][0]
            self.assertEqual(row["prompt"], mine["prompt"])
            self.assertEqual(row["key"]["worked"], mine["answer"]["worked"])
            self.assertEqual(tx.val("SELECT count(*) FROM ai_usage"), 0, "no AI calls")
        draft = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                              topic_ids=[school.topics["11.8"]], kind="numeric", difficulty="medium", count=1,
                                              title="Own")
        with self.db.tenant(school.inst) as tx:
            own = questions.add_own(tx, school.teacher, draft, topic_id=school.topics["11.8"], kind="numeric",
                                    prompt="A force of {f} N acts on {a} m². Find the pressure.", formula="f/a",
                                    vars_text="f = 100..500 step 50, a = 2..5", unit="Pa", tolerance="1",
                                    steps=["Pressure = force ÷ area", "Divide {f} by {a}"], worked="{f} ÷ {a} = {answer} Pa")
            q = tx.one("SELECT prompt, answer, status FROM questions WHERE id=%s", [own])
            self.assertNotIn("{", q["prompt"])
            self.assertEqual(q["status"], "approved")
            self.assertEqual(q["answer"]["template"]["formula"], "f/a")

    def test_number_pages(self):
        """[MA-4][UX-1][NF-A11Y] The question page has a number box (decimal keypad on phones) with buttons for ² ³ √ π
        × ÷; the teacher's draft shows the answer, unit, hints and the formula with sample numbers; the scorecard
        shows the worked solution."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        aid = self.open_assignment(school, kind="numeric", topics=("11.8",), count=1)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}?view=questions")[1]
            for text in ("Number problem", "Answer", "Hints", "Each student gets their own numbers"):
                self.assertIn(text, html)
            with self.db.tenant(school.inst) as tx:
                att = attempts.start_or_resume(tx, school.student(0), aid)
            st = Client(live.url)
            st.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = st.get(f"/student/attempts/{att['id']}")[1]
            self.assertIn('inputmode="decimal"', html)
            for sym in ("²", "√", "π", "×", "÷"):
                self.assertIn(f'data-sym="{sym}"', html)
        finally:
            live.stop()


class ClaudeNumberProblems(NQTestCase):
    def test_claude_number_problems_checked(self):
        """[MA-3][NF-AI] When Claude writes number problems, each one is checked before a teacher sees it: its
        formula must work for every student's numbers, every number range must appear in the question, and its
        hints must not give the answer. Ones that fail are dropped and NovaQuest's own problems fill in."""
        import json as _json

        from novaquest.ai.claude import ClaudeEngine
        from novaquest.ai.provider import ClaudeProvider
        from novaquest.services.context import topic_ctx
        from tests.test_ai import FakeServer, claude_reply

        good = {"topic_code": "11.8", "prompt": "A girl of weight {w} N stands on one foot of area {a} m². Find the pressure.",
                "vars": {"w": [300, 600, 50], "a": [0.01, 0.02, 0.01]}, "formula": "w/a", "unit": "Pa", "whole": True,
                "steps": ["Pressure = force ÷ area.", "Divide {w} by {a}."], "worked": "{w} ÷ {a} = {answer} Pa"}
        broken = {**good, "prompt": "Find the pressure with {w} N.", "formula": "w/zz"}
        leaky = {**good, "prompt": "A crate of {w} N rests on {a} m².", "steps": ["The answer is {answer} Pa."]}
        server = FakeServer(lambda b, h: claude_reply(_json.dumps({"questions": [good, broken, leaky]})))
        school = self.make_school(n_students=1)
        try:
            eng = ClaudeEngine(ClaudeProvider("k", server.url, "f", "s", timeout_s=2, max_retries=0))
            with self.db.tenant(school.inst) as tx:
                ctx = topic_ctx(tx, school.topics["11.8"])
            drafts = eng.generate_questions([ctx], "numeric", "medium", 2)
        finally:
            server.close()
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[0].written_by, "claude")
        self.assertIn("one foot", drafts[0].prompt)
        self.assertNotIn("{", drafts[0].prompt)
        self.assertEqual(drafts[0].answer["template"]["formula"], "w/a")
        self.assertEqual(drafts[1].written_by, "offline", "the two bad ones were dropped; a ready-made one filled in")
        self.assertIn("number problems", server.calls[0]["body"]["system"][0]["text"])
