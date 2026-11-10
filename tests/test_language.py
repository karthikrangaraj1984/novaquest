"""1.22.0: students' own language: the app's words, NOVA's replies and questions."""
from __future__ import annotations

import json

from novaquest import i18n
from novaquest.core.errors import ValidationError
from novaquest.services import attempts, language, learn, teach
from tests.base import PASSWORD, NQTestCase


class StubTranslator:
    """Stands in for Claude: marks each text with the language, and counts calls."""

    name = "claude"

    def __init__(self, real):
        self.real, self.calls = real, []

    def translate(self, texts, lang):
        self.calls.append((list(texts), lang))
        if lang == "English":
            return ["What does the cell membrane do?" for _ in texts]
        return [f"[{lang}] {t}" for t in texts]

    def __getattr__(self, item):
        return getattr(self.real, item)


class Languages(NQTestCase):
    def test_ui_words_and_setting(self):
        """[LG-1][UX-1] A student picks their language on Me (nine Indian languages and English). In Hindi or Tamil
        the menus, buttons and main screens (Home, Tasks, assignments, Daily 5) are in that language; in the other
        languages those words stay in English while NOVA uses the language. An unknown language is refused."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=2)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                language.set_lang(tx, school.student(0), "xx")
        t = i18n.translator("hi")
        self.assertEqual(t("Question {n} of {m}", n=2, m=5), "सवाल 2 / 5")
        self.assertEqual(i18n.translator("te")("Home"), "Home")
        self.assertEqual(set(i18n._HI), set(i18n._TA), "Hindi and Tamil cover the same words")
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login_student("SUN", "8B", "1", school.students[0]["pin"])
            me = c.get("/student/progress")[1]
            for code in i18n.LANGS:
                self.assertIn(f'value="{code}"', me)
            self.assertIn("தமிழ்", me)
            c.post("/student/language", {"lang": "hi"})
            home = c.get("/student")[1]
            for word in ("होम", "सीखें", "नमस्ते", "मैं"):
                self.assertIn(word, home)
            self.assertIn('<html lang="hi-IN">', home)
            att = c.get(f"/student/assignments/{aid}")[1]
            self.assertIn("संकेत चाहिए?", att)
            self.assertIn("जमा करें", att)
            c.post("/student/language", {"lang": "ta"})
            self.assertIn("முகப்பு", c.get("/student")[1])
            c.post("/student/language", {"lang": "te"})
            home = c.get("/student")[1]
            self.assertIn(">Home<", home.replace("</svg>", ">"))
        finally:
            live.stop()

    def test_nova_and_questions_in_the_students_language(self):
        """[LG-2][LG-3] In another language, what NOVA says (Ask NOVA, hints, Teach NOVA) is translated by Claude, and
        each question can be read in it ('Read in हिन्दी'); everything underneath stays English, so marking and NOVA's
        safety checks work as before. A question asked in Hindi is put into English for the textbook search. Each
        sentence is translated once and kept. Without Claude, NOVA stays in English."""
        school = self.make_school(n_students=1)
        st = school.student(0)
        eng = StubTranslator(self.engine)
        with self.db.tenant(school.inst) as tx:
            language.set_lang(tx, st, "hi")
        r1 = learn.ask(self.db, eng, st, "What does the cell membrane do?", school.topics["8.5"])
        self.assertTrue(r1.text.startswith("[Hindi] "), r1.text)
        n = len(eng.calls)
        r2 = learn.ask(self.db, eng, st, "What does the cell membrane do?", school.topics["8.5"])
        self.assertEqual(r1.text, r2.text)
        self.assertEqual(len(eng.calls), n, "kept: not translated twice")
        learn.ask(self.db, eng, st, "कोशिका झिल्ली क्या करती है?", school.topics["8.5"])
        self.assertIn(("कोशिका झिल्ली क्या करती है?",), [tuple(c[0]) for c in eng.calls if c[1] == "English"])
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=2)
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, st, aid)
            q = attempts.view(tx, st, att["id"])["questions"][0]
        h = attempts.hint(self.db, eng, st, att["id"], q["id"])
        self.assertTrue(h.text.startswith("[Hindi] "))
        with self.db.tenant(school.inst) as tx:
            stored = tx.val("""SELECT body FROM tutor_messages WHERE attempt_id=%s AND role='tutor' ORDER BY id DESC LIMIT 1""",
                            [att["id"]])
        self.assertFalse(stored.startswith("[Hindi]"), "kept in English underneath, for teachers and checks")
        tq = attempts.translate_question(self.db, eng, st, att["id"], q["id"])
        self.assertEqual(tq["prompt"], f"[Hindi] {q['prompt']}")
        self.assertEqual(tq["options"], [f"[Hindi] {o}" for o in q["options"]])
        with self.db.tenant(school.inst) as tx:
            sid = teach.start(tx, st, school.topics["8.5"])["session"]["id"]
        out = teach.turn(self.db, eng, st, sid, "Only plant cells have a cell wall.")
        self.assertTrue(out["reply"].startswith("[Hindi] "))
        with self.db.tenant(school.inst) as tx:
            view = teach.session_view(tx, st, sid)
            shown = language.localise(self.db, eng, school.inst, [t["body"] for t in view["turns"] if t["role"] == "nova"], "hi")
        self.assertTrue(all(s.startswith("[Hindi] ") for s in shown))
        plain = learn.ask(self.db, self.engine, st, "Who discovered the cell?", school.topics["8.1"])
        self.assertFalse(plain.text.startswith("["), "without Claude (nothing kept for this reply): English")

    def test_claude_translation_contract(self):
        """[LG-2][NF-AI] Claude is asked to translate for a school student, keeping numbers, units, formulas and
        names, with the English science word in brackets; a reply with the wrong number of texts is not used."""
        from novaquest.ai.claude import ClaudeEngine
        from novaquest.ai.provider import ClaudeProvider
        from tests.test_ai import FakeServer, claude_reply

        replies = [json.dumps({"texts": ["कोशिका झिल्ली (cell membrane) …", "दूसरा"]}), json.dumps({"texts": ["एक"]})]
        server = FakeServer(lambda b, h: claude_reply(replies.pop(0)))
        try:
            eng = ClaudeEngine(ClaudeProvider("k", server.url, "f", "s", timeout_s=2, max_retries=0))
            got = eng.translate(["The cell membrane …", "Second"], "Hindi")
            bad = eng.translate(["One", "Two"], "Hindi")
        finally:
            server.close()
        self.assertEqual(got, ["कोशिका झिल्ली (cell membrane) …", "दूसरा"])
        self.assertIsNone(bad)
        system = server.calls[0]["body"]["system"][0]["text"]
        self.assertIn("brackets", system)
        self.assertEqual(json.loads(server.calls[0]["body"]["messages"][0]["content"])["language"], "Hindi")
