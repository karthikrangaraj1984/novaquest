"""1.26.0: unit prep packs: a teacher prepares one chapter (lesson plan by period, mix-ups to watch for, the book's
activities as demos, key terms, an opening quiz, a PDF and their own notes), from the book or written by Claude."""
from __future__ import annotations

import json
import re
from datetime import date

from novaquest.ai import factory
from novaquest.ai.claude import ClaudeEngine
from novaquest.ai.provider import ClaudeProvider
from novaquest.core.errors import NotFound
from novaquest.services import plans, prep, questions
from tests.base import PASSWORD, NQTestCase
from tests.test_ai import FakeServer, claude_reply

# Text as NovaQuest stores it from NCERT Curiosity Grade 8, Chapter 2 (printer's footers and bullets included).
CHUNKS = [
    (1, 3, "Let us take a closer look at the basic structure of a cell using a microscope. Curiosity — Textbook of Science "
           "for Grade 8 Chapter 2.indd 10 6/30/2025 4:19:38 PM Activity 2.2: Let us study a cell (Teacher demonstration "
           "activity) Take an onion bulb from your kitchen or garden and wash it thoroughly with water. z Cut the onion bulb "
           "vertically into pieces. z Take one piece of onion and pull out the thin, transparent layer from its inner surface "
           "with the help of forceps. This layer is called the onion peel."),
    (2, 8, "Let us now take a closer look at the fascinating world of microbes. Activity 2.4: Let us observe pond water/ Take a "
           "container and collect pond or stagnant water in it with the help of your teacher or elder(s). z Use a dropper and "
           "place a drop of pond or stagnant water on a microscope slide. z Observe the tiny organisms found in the pond or "
           "stagnant water. Activity 2.5: Let us observe soil suspension Take a beaker and collect some moist soil in it from "
           "the nearby field or garden. Do not touch the soil with your bare hands — use a spoon or gloves. Pour some water "
           "into the beaker and stir it with a glass rod. What do you see in the water? The liquid, which may look dirty, has "
           "very fine particles of soil, and is called soil suspension. Scientists have studied soil for many years and "
           "found that it is full of living things."),
    (2, 9, "You may observe small moving organisms similar to those you saw in Activity 2.4. This indicates that even soil "
           "suspension contains a variety of tiny creatures."),
]


class PrepFromTheBook(NQTestCase):
    def _chapter(self, school):
        with self.db.tenant(school.inst) as tx:
            return tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])

    def test_book_activities(self):
        """[UP-2] The book's activity boxes ('Activity 2.2: Let us study a cell') are found in the chapter's text with
        their title, whether the book makes them a teacher demonstration, what to take, the steps, the questions they
        ask and their safety notes; mentions of an activity elsewhere ('…you saw in Activity 2.4') aren't new ones, and
        printers' footers are left out."""
        acts = prep.book_activities(CHUNKS)
        self.assertEqual([a["ref"] for a in acts], ["2.2", "2.4", "2.5"])
        cell, pond, soil = acts
        self.assertEqual(cell["title"], "Let us study a cell")
        self.assertEqual(cell["kind"], "Teacher demonstration")
        self.assertEqual(pond["kind"], "Student activity")
        self.assertEqual(pond["title"], "Let us observe pond water")
        self.assertTrue(cell["steps"][0].startswith("Take an onion bulb"))
        self.assertIn("Cut the onion bulb vertically into pieces.", cell["steps"])
        self.assertTrue(all("indd" not in s for a in acts for s in a["steps"]))
        self.assertIn("gloves", soil["safety"])
        self.assertIn("What do you see in the water?", soil["ask"])
        self.assertNotIn("Scientists", " ".join(soil["steps"]), "stops at the book's explanation")
        self.assertEqual((cell["topic_id"], cell["page"], soil["topic_id"]), (1, 3, 2))
        self.assertTrue(any(m.startswith("a beaker") for m in soil["materials"]))

    def test_pack_and_lesson_plan_from_the_book(self):
        """[UP-1] Without Claude, a teacher's pack is built from the book: for each topic an objective, an opening hook,
        3-5 key points, textbook pages, a check question with its answer and the mix-ups to watch for; the chapter's
        mix-ups with the right idea and how to spot them. The topics are shared over the periods the teacher has
        (the school days the term plan gives the chapter, or chosen), each with a timed flow that fills the period:
        fewer periods put neighbouring topics together; more add an activities period, a review and practice."""
        school = self.make_school(n_students=1)
        ch = self._chapter(school)
        with self.db.tenant(school.inst) as tx:
            data, row = prep.pack(tx, ch)
            self.assertIsNone(row)
            self.assertEqual(data["written_by"], "book")
            n = len(data["topics"])
            self.assertGreaterEqual(n, 3)
            for t in data["topics"]:
                self.assertTrue(t["objective"] and t["hook"], t)
                self.assertTrue(1 <= len(t["key_points"]) <= 5, t["code"])
                self.assertTrue(t["pages"].startswith("p"), t)
            checks = [t["check"] for t in data["topics"]]
            self.assertTrue(all(c and c["q"] for c in checks))
            for c in checks:
                if c["options"]:  # the book's lesson check
                    self.assertTrue(0 <= c["answer"] < len(c["options"]) and len(set(c["options"])) == len(c["options"]))
                else:  # asked again at the end, listening for the key points
                    self.assertTrue(c["q"].endswith("?") and c["listen"])
            self.assertTrue(data["mixups"], "the book's approved mix-ups")
            self.assertTrue(all(m["right"] for m in data["mixups"]))
            self.assertTrue(any(t["watch_for"] for t in data["topics"]))
            self.assertEqual(prep.default_periods(tx, school.class_id, ch, data), n + (1 if data["activities"] else 0) + 1)
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id, [(ch, date(2026, 11, 2), date(2026, 11, 7))])
            self.assertEqual(prep.default_periods(tx, school.class_id, ch, data), 6)
        few = prep.arrange(data, 2, 40)
        self.assertEqual(len(few), 2)
        self.assertEqual(sum(len(p["topics"]) for p in few), n)
        many = prep.arrange(data, n + 3, 45)
        self.assertEqual(len(many), n + 3)
        self.assertIn("review", [p["kind"] for p in many])
        self.assertEqual({sum(m for m, _ in p["flow"]) for p in few}, {40})
        self.assertEqual({sum(m for m, _ in p["flow"]) for p in many}, {45})

    def test_teacher_page_pdf_notes_and_opening_quiz(self):
        """[UP-4][UX-1] Teachers open 'Prepare to teach' from the Syllabus page or the term plan: the chapter in class
        now opens first. They can download it as a PDF, keep their own notes on the chapter (printed in their PDF,
        not seen by others) and make a five-question opening quiz across the chapter in one click (a draft to check,
        then schedule or show on the projector). Principals can read the pack. Another school can't open it."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        ch = self._chapter(school)
        other = self.make_school(code="MOON", n_students=1)
        with self.db.tenant(school.inst) as tx:
            plans.save(tx, school.teacher, school.class_id, school.syllabus_id, [(ch, date(2026, 10, 1), date(2026, 12, 20))])
        with self.db.tenant(other.inst) as tx:
            tx.exec("INSERT INTO unit_notes(institution_id, teacher_id, chapter_id, body, updated_at) VALUES (%s,%s,%s,'x',now())",
                    [other.inst, other.teacher.user_id, ch])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertIn("/teacher/prep?for=", t.get("/teacher/syllabus")[1])
            self.assertIn("Prepare</a>", t.get("/teacher/plan")[1])
            page = t.get("/teacher/prep")[1]
            self.assertIn("Lesson plan", page)
            self.assertIn("Mix-ups to watch for", page)
            self.assertIn("Period 1:", page)
            self.assertIn("from the school days your term plan gives", page)
            t.post(f"/teacher/prep/{ch}/notes", {"for": f"{school.class_id}:{school.syllabus_id}", "notes": "Bring onions on Monday"})
            self.assertIn("Bring onions on Monday", t.get(f"/teacher/prep?chapter={ch}")[1])
            status = t.get(f"/teacher/prep?chapter={ch}&periods=3&minutes=35&format=pdf")[0]
            self.assertEqual(status, 200)
            self.assertTrue(t.last_bytes.startswith(b"%PDF"))
            t.post(f"/teacher/prep/{ch}/quiz", {"for": f"{school.class_id}:{school.syllabus_id}"})
            with self.db.tenant(school.inst) as tx:
                aid = tx.val("SELECT id FROM assignments WHERE title LIKE 'Opening quiz:%%' ORDER BY id DESC LIMIT 1")
                self.assertIsNotNone(aid)
                self.assertEqual(len(questions.assignment_questions(tx, aid)), 5)
                self.assertEqual(tx.val("SELECT state FROM assignments WHERE id=%s", [aid]), "draft")
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            seen = p.get(f"/teacher/prep?chapter={ch}")[1]
            self.assertIn("Lesson plan", seen)
            self.assertNotIn("Bring onions", seen, "notes are the teacher's own")
            self.assertNotIn("Make the opening quiz", seen)
        finally:
            live.stop()
        with self.db.tenant(other.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM unit_notes"), 1)
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM unit_notes"), 1, "each school sees only its own notes")
            with self.assertRaises(NotFound):
                prep.save_notes(tx, school.teacher, 10 ** 9, "x")


class PrepByClaude(NQTestCase):
    def test_claude_pack(self):
        """[UP-3][NF-AI] With Claude, opening a chapter asks Claude (in the background) to write the pack from the book,
        once per school: hooks, key points, a check per topic, mix-ups with how to spot and fix them, and the book's
        activities written up with materials, steps, questions and safety. Claude is told to use only the book and to
        add only safe demos with everyday materials. Its reply is checked: unknown topics and broken check questions
        are dropped (the book's are used), and none of the book's activities is lost. If Claude fails, the book's pack
        stays and the teacher can try again."""
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            ch = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])
            book = prep.from_book(tx, ch)
        codes = [t["code"] for t in book["topics"]]
        reply = {"overview": "Cells are the building blocks of life.",
                 "topics": [{"code": codes[0], "objective": "Describe what a cell is.", "hook": "Show a brick wall: what is it made of?",
                             "key_points": ["All living things are made of cells.", "A cell is the basic unit of life."],
                             "activity": "Look at onion peel cells.",
                             "check": {"q": "What is the basic unit of life?", "options": ["Cell", "Tissue", "Organ", "Atom"],
                                       "answer": 0, "why": "Every living thing is made of cells."},
                             "watch_for": ["Only plants have cells"]},
                            {"code": codes[1], "hook": "Ask about eggs.", "check": {"q": "Broken", "options": ["A", "A"], "answer": 5}},
                            {"code": "99.9", "hook": "Not in this chapter."}],
                 "mixups": [{"label": "Only plants have cells", "wrong": "Animals are not made of cells.",
                             "right": "Plants and animals are both made of cells.", "spot": "Students draw only plant cells.",
                             "fix": "Show cheek cells beside onion cells.", "topic": codes[0]}],
                 "activities": [{"ref": None, "title": "Bricks and walls", "kind": "Discussion", "materials": ["A picture of a wall"],
                                 "steps": ["Show the wall.", "Ask what it is made of."], "ask": ["What are you made of?"], "safety": None,
                                 "topic": codes[0]}],
                 "terms": [{"term": "Cell", "meaning": "The basic unit of life."}]}
        calls: list[str] = []
        mode = {"fail": False, "cut": set()}

        def respond(body, headers):
            ask = body["messages"][0]["content"]
            calls.append(ask)
            if mode["fail"]:
                return claude_reply("not json")
            if '"overview"' in ask:
                part = {k: reply[k] for k in ("overview", "mixups", "terms")}
            elif '"topics"' in ask:
                wanted = re.findall(r"\d+\.\d+", ask.split("for topic", 1)[1])
                if set(wanted) & mode["cut"]:  # this reply is cut off at the length limit
                    status, payload = claude_reply('{"topics": [{"code": "' + wanted[0] + '", "hook": "Half a')
                    payload["stop_reason"] = "max_tokens"
                    return status, payload
                part = {"topics": [t for t in reply["topics"] if t["code"] in wanted] + ([reply["topics"][2]] if codes[0] in wanted else [])}
            else:
                part = {"activities": reply["activities"] if "your own" in ask else []}
            return claude_reply(json.dumps(part))
        server = FakeServer(respond)
        try:
            factory.set_provider(ClaudeProvider("k", server.url, "f", "s", timeout_s=2, max_retries=0))
            from tests.live import Client, LiveServer
            live = LiveServer(self.settings, self.db)
            try:
                t = Client(live.url)
                t.login("rao@sun.test", PASSWORD)
                page = t.get(f"/teacher/prep?chapter={ch}")[1]
                self.assertIn("Claude is writing a fuller pack", page)
            finally:
                live.stop()
            with self.db.tenant(school.inst) as tx:
                self.assertEqual(tx.val("SELECT status FROM unit_packs WHERE chapter_id=%s", [ch]), "writing")
                self.assertEqual(tx.val("SELECT count(*) FROM jobs WHERE kind=%s", [prep.JOB]), 1)
            eng = factory.get_engine(self.settings, self.db, school.inst)
            self.assertIsInstance(eng, ClaudeEngine)
            mode["cut"] = {codes[-1]}  # one part's reply is cut off at the length limit
            prep.write_with_claude(self.db, eng, school.inst, ch)
            with self.db.tenant(school.inst) as tx:
                data, row = prep.pack(tx, ch)
            self.assertEqual(row["status"], "ready")
            self.assertEqual(data["written_by"], "claude")
            first, second = data["topics"][0], data["topics"][1]
            self.assertEqual(first["hook"], "Show a brick wall: what is it made of?")
            self.assertEqual(first["check"]["q"], "What is the basic unit of life?")
            self.assertEqual(second["hook"], "Ask about eggs.")
            self.assertEqual(second["check"], book["topics"][1]["check"], "a broken check is replaced by the book's")
            self.assertEqual([t["code"] for t in data["topics"]], codes, "no topic Claude invented")
            self.assertEqual(data["mixups"][0]["fix"], "Show cheek cells beside onion cells.")
            self.assertEqual(data["activities"][0]["title"], "Bricks and walls")
            self.assertEqual(len(data["activities"]), 1 + len(book["activities"]))
            # written a part at a time (no reply long enough to be cut off); the chapter's text is cached across parts
            topic_asks = [a for a in calls if '"topics"' in a]
            self.assertEqual(len(topic_asks), (len(codes) + 1) // 2)
            self.assertTrue(all(len(re.findall(r"\d+\.\d+", a.split("for topic", 1)[1])) <= 2 for a in topic_asks))
            self.assertIn("cache_control", server.calls[0]["body"]["system"][1])
            # the part that was cut off comes from the book, and the page says so
            self.assertEqual(data["topics"][-1]["hook"], book["topics"][-1]["hook"])
            self.assertEqual(data["partial"], ["topics"])
            system = server.calls[0]["body"]["system"][0]["text"]
            self.assertIn("safe everyday classroom materials", system)
            self.assertIn("Every fact must come from the textbook", system)
            # a failed rewrite keeps the last good pack and says why in plain words
            mode["fail"] = True
            with self.db.tenant(school.inst) as tx:
                prep.request_claude(tx, school.teacher, ch)
            prep.write_with_claude(self.db, eng, school.inst, ch)
            with self.db.tenant(school.inst) as tx:
                data2, row2 = prep.pack(tx, ch)
            self.assertEqual(row2["status"], "failed")
            self.assertEqual(data2["topics"][0]["hook"], first["hook"])
            self.assertEqual(row2["error"], "Claude's reply wasn't complete.")
            with self.db.tenant(school.inst) as tx:  # an earlier version stored the technical reason
                tx.exec("UPDATE unit_packs SET error=%s WHERE chapter_id=%s",
                        ["bad JSON: Unterminated string starting at: line 1 column 18647 (char 18646)", ch])
                self.assertEqual(prep.stored(tx, ch)["error"], "Claude's reply wasn't complete.")
        finally:
            factory.set_provider(None)
            server.close()
