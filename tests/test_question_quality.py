"""Question quality without Claude, measured over every topic of the book (teacher review, items 4 and 5)."""
from __future__ import annotations

import re

from novaquest.ai.offline import OfflineEngine, _base, _glued_heading, _is_name
from novaquest.services import questions
from novaquest.services.context import topic_ctx
from tests.base import NQTestCase


class OfflineQuestionQuality(NQTestCase):
    def setUp(self):
        super().setUp()
        self.school = self.make_school()
        with self.db.tenant(self.school.inst) as tx:
            self.ctxs = [topic_ctx(tx, tid) for tid in self.school.topics.values()]
        self.eng = OfflineEngine()

    def test_difficulty_means_deeper_thinking_across_the_book(self):
        """[AS-4][AS-2] Over all 125 topics: easy questions test recall; medium ones are mostly 'why', 'what happens if'
        and 'which statement is correct' questions (at least 80% beyond recall); hard ones mostly ask to analyse (at
        least 60%), such as spotting the incorrect statement or correcting NOVA's mix-up. Weak 'which key word belongs'
        questions appear only as an easy fallback."""
        levels = {"easy": [], "medium": [], "hard": []}
        for c in self.ctxs:
            for diff in levels:
                for q in self.eng.generate_questions([c], "mcq", diff, 3, seed=c.id):
                    levels[diff].append(q)
                    if diff != "easy":
                        self.assertNotIn("Which key word belongs", q.prompt)
        self.assertTrue(all(q.thinking_level == "remember" for q in levels["easy"]))
        medium = levels["medium"]
        self.assertGreaterEqual(sum(q.thinking_level != "remember" for q in medium) / len(medium), 0.8)
        hard = levels["hard"]
        self.assertGreaterEqual(sum(q.thinking_level == "analyse" for q in hard) / len(hard), 0.6)
        kinds = {re.sub(r"“.*", "", q.prompt)[:24] for q in medium + hard}
        self.assertTrue({"What is the reason? ", "Which statement about "} <= {k[:len(x)] for k in kinds for x in
                                                                              ("What is the reason? ", "Which statement about ")})

    def test_options_are_believable_and_never_two_right_answers(self):
        """[AS-4] Every multiple-choice question has 4 different options with exactly one marked right; wrong options
        are never a near-copy of the answer ('weeds'/'weeding'), and never a name offered for an ordinary word.
        Prompts never contain a section heading glued onto a sentence."""
        checked = 0
        for c in self.ctxs:
            for diff in ("easy", "medium", "hard"):
                for q in self.eng.generate_questions([c], "mcq", diff, 2, seed=c.id + 1):
                    checked += 1
                    self.assertEqual(len(set(q.options)), len(q.options), q.prompt)
                    self.assertEqual(q.options[q.answer["index"]], q.answer["text"])
                    if q.prompt.startswith("Fill in the blank"):
                        sentence = q.prompt.replace("Fill in the blank: ", "")
                        self.assertFalse(_glued_heading(sentence), sentence)
                        right = q.answer["text"]
                        for o in q.options:
                            if o != right:
                                self.assertNotEqual(_base(o), _base(right), q.prompt)
                                self.assertFalse(_base(o).startswith(_base(right)) or _base(right).startswith(_base(o)),
                                                 (o, right))
                                self.assertEqual(_is_name(o), _is_name(right), (o, right))
        self.assertGreater(checked, 500)

    def test_statement_questions_change_one_word_and_read_naturally(self):
        """[AS-4] In 'which statement is correct' questions the wrong statements differ from the textbook sentence by
        one swapped key word of the same kind, start with a capital letter, and aren't story lines or instructions."""
        seen = 0
        for c in self.ctxs:
            for q in self.eng.generate_questions([c], "mcq", "medium", 3, seed=c.id):
                if not q.prompt.endswith("is correct?") or q.misconception_codes:
                    continue  # (questions about a common mix-up share the wording but list whole ideas)
                seen += 1
                right = q.answer["text"]
                self.assertNotRegex(right, r"\b(explains that|tells them|recall that)\b")
                for o in q.options:
                    self.assertTrue(o[:1].isupper(), o)
                    if o != right:
                        a, b = right.split(), o.split()
                        diff = sum(1 for x, y in zip(a, b) if x != y)
                        self.assertLessEqual(diff, 3, (right, o))
        self.assertGreater(seen, 40)

    def test_written_answers_have_clean_models_and_mission_stages_differ(self):
        """[AS-4][LN-3] Written-answer questions have clean model answers (no glued headings, no broken fragments) and
        marking points that state the idea; topics titled as questions don't produce doubled question marks; each
        mission stage has its own story line, the first introducing the mission."""
        for c in self.ctxs:
            for q in self.eng.generate_questions([c], "short", "medium", 3, seed=c.id):
                self.assertNotIn("??", q.prompt)
                model = q.answer["model"]
                self.assertNotRegex(model, r"\.\s+[a-z]", model)
                for sent in re.split(r"(?<=\.)\s+", model):
                    if len(sent.split()) >= 8:
                        self.assertFalse(_glued_heading(sent), sent)
        mission = self.eng.generate_questions(self.ctxs[30:32], "mission", "medium", 5, seed=4)
        stories = [re.sub(r"^Mission stage \d+ of \d+\.\s*", "", q.scenario) for q in mission]
        self.assertTrue(stories[0].startswith("The NovaQuest lab robot is stuck"))
        robot = [s for s in stories if not s.startswith("An AI chatbot")]
        self.assertEqual(len(set(robot)), len(robot), stories)

    def test_regenerate_gives_a_different_question_with_the_same_wording(self):
        """[AS-5] Regenerate replaces a question even when the new one shares its wording ('Which statement about … is
        correct?') but tests a different fact."""
        s = self.school
        aid = questions.generate_assignment(self.db, self.engine, s.teacher, class_id=s.class_id, topic_ids=[s.topics["2.5"]],
                                            kind="quiz", difficulty="medium", count=3, title="")
        with self.db.tenant(s.inst) as tx:
            before = questions.assignment_questions(tx, aid)
        new_id = questions.regenerate(self.db, self.engine, s.teacher, before[0]["id"])
        with self.db.tenant(s.inst) as tx:
            after = {q["id"]: q for q in questions.assignment_questions(tx, aid)}
        self.assertIn(new_id, after)
        old_keys = {(q["prompt"], q["answer"]["text"]) for q in before}
        self.assertNotIn((after[new_id]["prompt"], after[new_id]["answer"]["text"]), old_keys)

    def test_reason_questions_have_one_right_answer(self):
        """[AS-4] In 'What is the reason?' and 'What happens if?' questions the wrong options come from other topics
        and share no key word with this topic's reasons, so there are never two right answers (a teacher found
        'To check the growth of bacteria' offered as wrong next to 'To prevent the attack of microbes'), and are never
        vague ('To get sick.')."""
        from novaquest.ai.offline import _REASON, _clauses, _content_words, _vague

        seen = 0
        for c in self.ctxs:
            own = {w for kind, _ in _REASON for cl in _clauses([p["body"] for p in c.passages], kind) for w in _content_words(cl[2])}
            own |= {w for term in c.terms for w in _content_words(term)}
            for q in self.eng.generate_questions([c], "mcq", "medium", 4, seed=c.id):
                if not q.prompt.startswith(("What is the reason?", "What happens if", "Why?")):
                    continue
                seen += 1
                for o in q.options:
                    if o != q.answer["text"]:
                        self.assertFalse(_content_words(o) & own, (q.prompt, o))
                        self.assertFalse(_vague(o.rstrip(".")), o)
        self.assertGreaterEqual(seen, 6)  # (1.7.1: vague wrong reasons like 'To get sick.' are no longer offered, so fewer qualify)


class NoGiveawaysOrNonsense(NQTestCase):
    def test_blanks_dont_give_the_answer_away_and_statements_read_sensibly(self):
        """[AS-4] Over every topic: a blank never has its answer (or part of it) elsewhere in the sentence ('_____
        (pseudo: false; podia: feet)'), no question leans on an earlier class or the sentence before ('as you learnt
        in Class VII', 'As a result, …'), no story or first-person line becomes a question, 'shape' or 'size' is
        never swapped for a science word, articles agree after a swap ('an embryo'), and topic titles that are
        questions read cleanly ('about do liquids conduct electricity is correct?', never '…electricity? is …')."""
        from novaquest.ai.offline import _ATTRIBUTE, _ELSEWHERE, _LEANS, _NARRATION, _leaks

        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            ctxs = [topic_ctx(tx, tid) for tid in school.topics.values()]
        eng = OfflineEngine()
        blanks = statements = 0
        for c in ctxs:
            for diff in ("easy", "medium", "hard"):
                for q in eng.generate_questions([c], "mcq", diff, 3, seed=c.id):
                    self.assertNotIn("? is", q.prompt)
                    if q.prompt.startswith("Fill in the blank: "):
                        blanks += 1
                        sentence = q.prompt.replace("Fill in the blank: ", "").replace("_____", q.answer["text"])
                        self.assertFalse(_leaks(sentence, q.answer["text"]), q.prompt)
                        for bad in (_ELSEWHERE.search(sentence), _LEANS.match(sentence), _NARRATION.search(sentence)):
                            self.assertIsNone(bad, q.prompt)
                        self.assertNotIn(q.answer["text"].lower(), _ATTRIBUTE)
                    elif q.prompt.endswith("is correct?") and not q.misconception_codes:
                        statements += 1
                        for o in q.options:
                            self.assertNotRegex(o, r"(^A|\ba) [aeiou]\w|(^An|\ban) [b-df-hj-np-tv-z]\w", o)
                            self.assertIsNone(_LEANS.match(o), o)
                            self.assertIsNone(_NARRATION.search(o), o)
                        right = q.answer["text"].split()
                        for o in q.options:
                            if o != q.answer["text"]:
                                swapped = [w for w, x in zip(right, o.split()) if w != x]
                                self.assertFalse(swapped and swapped[0].lower().strip(",.") in _ATTRIBUTE, (q.answer["text"], o))
        self.assertGreater(blanks, 250)
        self.assertGreater(statements, 150)
