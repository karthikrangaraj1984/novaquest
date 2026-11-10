"""1.13.0: Sign out at the top right for staff; teachers' messages to students (a note to the class with the results,
notes to one student that reach their bell, quick starters and a note suggested from the answers); progress reports
emailed to parents; Claude use on School setup; teachers' own questions."""
from __future__ import annotations

import asyncio
import json
import re

from novaquest.ai.claude import ClaudeEngine
from novaquest.ai.offline import OfflineEngine
from novaquest.ai.provider import ClaudeProvider
from novaquest.core.errors import Conflict, ValidationError
from novaquest.services import attempts, questions, reports, review
from tests.base import PASSWORD, NQTestCase
from tests.test_ai import FakeServer, claude_reply


class AccountMenu(NQTestCase):
    def test_sign_out_is_at_the_top_right(self):
        """[UX-1][ON-3] On a laptop every staff page (teacher, school admin, principal) has an account button at the top
        right showing the person's name; it opens Sign out (and Switch role for people with two roles). The side menu
        no longer carries Sign out. On a phone the same menu sits in the top bar."""
        from playwright.async_api import async_playwright

        from tests.live import LiveServer
        from tests.test_browser import A11Y_JS

        school = self.make_school()
        live = LiveServer(self.settings, self.db)

        async def go():
            out = {}
            async with async_playwright() as p:
                b = await p.chromium.launch()
                for who, email, path in (("teacher", "rao@sun.test", "/teacher"), ("admin", "admin@sun.test", "/admin"),
                                         ("principal", "principal@sun.test", "/leader")):
                    for name, size in (("laptop", (1366, 800)), ("phone", (390, 844))):
                        page = await (await b.new_context(viewport={"width": size[0], "height": size[1]})).new_page()
                        await page.goto(live.url + "/login")
                        await page.fill("#email", email)
                        await page.fill("#password", PASSWORD)
                        await page.click("button[type=submit]")
                        await page.goto(live.url + path)
                        menu = ".topbar summary" if name == "laptop" else ".mtop summary"
                        box = await page.locator(menu).bounding_box()
                        await page.click(menu)
                        signout = page.locator(("" if name == "phone" else ".topbar ") + "button:has-text('Sign out')").first
                        visible = await signout.is_visible()
                        side = await page.locator("nav.side button:has-text('Sign out')").count()
                        a11y = await page.evaluate(A11Y_JS)
                        await signout.click()
                        await page.wait_for_load_state()
                        out[(who, name)] = {"box": box, "visible": visible, "side": side, "a11y": a11y, "url": page.url,
                                            "label": await page.locator("body").inner_text()}
                await b.close()
            return out

        try:
            out = asyncio.run(go())
        finally:
            live.stop()
        for (who, name), r in out.items():
            w = 1366 if name == "laptop" else 390
            self.assertGreater(r["box"]["x"] + r["box"]["width"], w - 80, f"{who} {name}: at the right edge")
            self.assertLess(r["box"]["y"], 90, f"{who} {name}: at the top")
            self.assertTrue(r["visible"], f"{who} {name}: Sign out shows when the menu opens")
            self.assertEqual(r["side"], 0, f"{who} {name}: not in the side menu any more")
            self.assertEqual(r["a11y"], [], f"{who} {name}")
            self.assertIn("/login", r["url"], f"{who} {name}: signed out")


class TeacherMessages(NQTestCase):
    def _handed_in(self, school, n=2):
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=4)
        atts = []
        for i in range(n):
            st = school.student(i)
            att = self.answer_all(school, st, aid, good=i == 0)
            attempts.submit(self.db, self.engine, st, att)
            atts.append(att)
        return aid, atts

    def _bell(self, school, i):
        with self.db.tenant(school.inst) as tx:
            return tx.all("SELECT kind, title, body, read_at FROM notifications WHERE user_id=%s ORDER BY id",
                          [school.student(i).user_id])

    def test_class_message_and_notes_reach_students(self):
        """[NR-5][AS-7] The teacher can write a message to the whole class when releasing results; every student who
        handed in sees it at the top of their results, and the release notification says it's there. A note to one
        student written after release reaches that student's bell ('Mrs Rao left you a note'); editing the class
        message after release notifies again. Students never see each other's notes."""
        school = self.make_school(n_students=3)
        aid, atts = self._handed_in(school)
        with self.db.tenant(school.inst) as tx:
            review.add_feedback(tx, school.teacher, atts[1], "Revise 8.5: the cell wall is only in plant cells.")
            self.assertEqual([n for n in self._bell(school, 1) if n["kind"] == "feedback"], [], "not before release")
            review.release(tx, school.teacher, aid, "Well done, everyone! Revise 8.3 before Friday.")
        for i, extra in ((0, "a message for the class."), (1, "a message for the class and a note for you.")):
            res = [n for n in self._bell(school, i) if n["kind"] == "results"]
            self.assertTrue(res[0]["body"].endswith(extra), res[0]["body"])
        with self.db.tenant(school.inst) as tx:
            card = review.scorecard(tx, school.student(1), aid)
            self.assertEqual(card["class_message"], "Well done, everyone! Revise 8.3 before Friday.")
            self.assertIn("cell wall", card["feedback"])
            self.assertIsNone(review.scorecard(tx, school.student(0), aid)["feedback"], "notes are private")
            review.add_feedback(tx, school.teacher, atts[0], "Excellent work on the cell!")
            review.add_feedback(tx, school.teacher, atts[0], "Excellent work on the cell!")  # unchanged: no new alert
            review.set_class_message(tx, school.teacher, aid, "Test on Friday covers 8.3 and 8.5.")
            with self.assertRaises(Exception):
                review.set_class_message(tx, school.student(0), aid, "hi")
        note = [n for n in self._bell(school, 0) if n["kind"] == "feedback"]
        self.assertEqual({n["title"] for n in note}, {"Mrs Rao left you a note", "Message from Mrs Rao"})
        self.assertTrue(all(n["read_at"] is None for n in note))
        self.assertIn("Friday covers 8.3", next(n["body"] for n in note if n["title"].startswith("Message")))
        self.assertEqual(len([n for n in self._bell(school, 2) if n["kind"] in ("feedback", "results")]), 0,
                         "a student who didn't hand in gets nothing")

    def test_messages_over_http_with_starters_and_a_suggested_note(self):
        """[NR-5][UX-1] Over HTTP: the review page has a 'Message to the class' box with quick starters beside Release
        results; after release the message can be updated. A student's page has a 'Note to …' box with starters and
        'Suggest a note from the answers', which fills the box from the student's results (not saved until the teacher
        saves). The student sees both messages on their results."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        aid, atts = self._handed_in(school)
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertIn('name="message"', html)
            self.assertIn('data-insert="cm"', html)
            html = t.post(f"/teacher/assignments/{aid}/release", {"message": "Great effort, 8B!"})[1]
            self.assertIn("Results released", html)
            self.assertIn("Great effort, 8B!", html)
            self.assertIn("Update message", html)
            sid = school.student(1).user_id
            html = t.get(f"/teacher/assignments/{aid}/students/{sid}")[1]
            self.assertIn("Note to Student", html)
            self.assertIn("Suggest a note from the answers", html)
            html = t.post(f"/teacher/attempts/{atts[1]}/suggest")[1]
            box = re.search(r'<textarea id="fb"[^>]*>([^<]*)</textarea>', html).group(1)
            self.assertTrue(box.startswith("Student,") and "revise" in box.lower(), box)
            self.assertIn("nothing is sent until you do", html)
            with self.db.tenant(school.inst) as tx:
                self.assertIsNone(tx.val("SELECT feedback FROM attempts WHERE id=%s", [atts[1]]), "a suggestion isn't saved")
            t.post(f"/teacher/attempts/{atts[1]}/feedback", {"feedback": box})
            s = Client(live.url)
            s.login_student("SUN", "8B", "2", school.students[1]["pin"])
            html = s.get(f"/student/results/{aid}")[1]
            self.assertIn("Mrs Rao, to the class", html)
            self.assertIn("Great effort, 8B!", html)
            self.assertIn("Mrs Rao, to you", html)
            self.assertIn("left you a note", s.get("/notifications")[1])
        finally:
            live.stop()

    def test_suggested_note_with_claude_and_offline(self):
        """[NR-5][NF-AI] The suggested note uses the student's results: Claude gets the first name, the topics with
        how well each went and the mix-ups, and writes 2-3 sentences; without Claude (or if it fails) NovaQuest's own
        note names a strong topic and the one to revise, and never praises a weak result."""
        info = {"first_name": "Diya", "title": "Cell Lab", "right": 2, "total": 5, "mixups": ["Cell wall vs membrane"],
                "topics": [{"name": "Cell structure", "pct": 90}, {"name": "Plant and animal cells", "pct": 20}]}
        off = OfflineEngine().suggest_note(info)
        self.assertTrue(off.startswith("Diya, well done on Cell structure"), off)
        self.assertIn("revise Plant and animal cells", off)
        weak = OfflineEngine().suggest_note({**info, "topics": [{"name": "Plant and animal cells", "pct": 10}]})
        self.assertNotIn("well done", weak.lower())
        server = FakeServer(lambda b, h: claude_reply("Diya, you explained cell parts clearly. Next, revise plant and "
                                                      "animal cells on Learn, with the pictures and practice."))
        try:
            note = ClaudeEngine(ClaudeProvider("k", server.url, "f", "s")).suggest_note(info)
        finally:
            server.close()
        self.assertTrue(note.startswith("Diya, you explained"))
        sent = json.loads(server.calls[0]["body"]["messages"][0]["content"])
        self.assertEqual(sent["topics"][1], {"name": "Plant and animal cells", "pct": 20})
        bad = FakeServer(lambda b, h: (500, {"error": "down"}))
        try:
            fallback = ClaudeEngine(ClaudeProvider("k", bad.url, "f", "s", max_retries=0)).suggest_note(info)
        finally:
            bad.close()
        self.assertEqual(fallback, off)


class ParentReports(NQTestCase):
    def test_reports_emailed_to_parents_with_consent(self):
        """[SX-6][NR-1] 'Email reports to parents' sends each parent with recorded consent and an email a private
        two-week link to their child's progress report; students without consent or without an email are skipped and
        counted. The report (web and PDF) now includes the teachers' notes. Sending twice the same day sends once."""
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=4, consent=False)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3",), count=3)
        st = school.student(0)
        att = self.answer_all(school, st, aid)
        attempts.submit(self.db, self.engine, st, att)
        with self.db.tenant(school.inst) as tx:
            review.add_feedback(tx, school.teacher, att, "Lovely clear answers about cells.")
            review.release(tx, school.teacher, aid)
            for i in (0, 1):
                tx.exec("UPDATE enrolments SET consent_status='recorded' WHERE student_id=%s", [school.student(i).user_id])
            tx.exec("UPDATE enrolments SET parent_email=NULL WHERE student_id=%s", [school.student(1).user_id])
            r = reports.email_parents(tx, school.teacher, school.class_id, "https://nq.test")
            self.assertEqual(r, {"sent": 1, "no_email": 1, "no_consent": 2})
            mail = tx.one("SELECT to_addr, subject, body FROM email_outbox WHERE dedupe_key LIKE 'parent-report:%%'")
            self.assertEqual(mail["to_addr"], "parent1@sun.test")
            self.assertEqual(mail["subject"], "Student's progress report")
            link = re.search(r"https://nq\.test(/r/\S+)", mail["body"]).group(1)
            reports.email_parents(tx, school.teacher, school.class_id, "https://nq.test")
            self.assertEqual(tx.val("SELECT count(*) FROM email_outbox WHERE dedupe_key LIKE 'parent-report:%%'"), 1)
            with self.assertRaises(Exception):
                reports.email_parents(tx, school.student(0), school.class_id, "https://nq.test")
        live = LiveServer(self.settings, self.db)
        try:
            html = Client(live.url).get(link)[1]
            self.assertIn("Lovely clear answers about cells.", html)
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/classes/{school.class_id}/students")[1]
            self.assertIn("Progress reports for parents", html)
            self.assertIn("Last sent", html)
        finally:
            live.stop()
        import io

        import pypdf
        with self.db.tenant(school.inst) as tx:
            pdf = reports.report_pdf(tx, school.teacher, st.user_id)
        text = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn("Notes from teachers", text)
        self.assertIn("Lovely clear answers", text)


class SchoolAiUse(NQTestCase):
    def test_admin_sees_claude_use(self):
        """[NF-COST] School setup shows the school's Claude use: today's tokens against the daily limit, the last 30
        days' requests and estimated cost, and how often the offline engine stood in; without a Claude key it says
        how to add one."""
        from novaquest.ai.factory import school_usage
        from novaquest.core import clock
        from tests.live import Client, LiveServer

        school = self.make_school()
        with self.db.tenant(school.inst) as tx:
            tx.exec("""INSERT INTO ai_usage(institution_id, day, tier, calls, input_tokens, output_tokens, cache_read_tokens, fallbacks)
                       VALUES (%s, %s, 'strong', 10, 200000, 100000, 0, 2), (%s, %s, 'fast', 40, 50000, 10000, 0, 1)""",
                    [school.inst, clock.now().date(), school.inst, clock.now().date()])
            tx.exec("UPDATE institutions SET ai_daily_token_cap=720000 WHERE id=%s", [school.inst])
        u = school_usage(self.db, self.settings, school.inst)
        self.assertEqual((u["today"], u["cap"], u["today_pct"], u["calls"], u["fallbacks"]), (360000, 720000, 50, 50, 3))
        self.assertEqual(u["cost_usd"], round((200000 * 3 + 100000 * 15 + 50000 * 1 + 10000 * 5) / 1e6, 2))
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get("/admin")[1]
            self.assertIn("Claude AI use", html)
            self.assertIn("nq set-key", html, "no Claude key in the tests: it says how to add one")
        finally:
            live.stop()


class OwnQuestions(NQTestCase):
    def test_teacher_adds_own_questions(self):
        """[AS-5][AS-6] A teacher adds their own multiple-choice or short-answer question to a draft, on one of its
        topics; it's approved as written, goes into the question bank, and students answer and are marked on it like
        any other. Incomplete questions are refused with a plain reason, and nothing can be added once scheduled."""
        from datetime import timedelta

        from novaquest.core import clock
        from novaquest.services import assignments
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=1)
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[school.topics["8.5"]], kind="quiz", difficulty="medium", count=2,
                                            title="Cells")
        topic = school.topics["8.5"]
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                questions.add_own(tx, school.teacher, aid, topic_id=topic, kind="mcq", prompt="Which cells have a wall?",
                                  options=["Plant cells", ""], answer_index=None)
            with self.assertRaises(ValidationError):
                questions.add_own(tx, school.teacher, aid, topic_id=school.topics["8.3"], kind="mcq", prompt="Off topic?",
                                  options=["a", "b"], answer_index=0)
            with self.assertRaises(ValidationError):
                questions.add_own(tx, school.teacher, aid, topic_id=topic, kind="short", prompt="Explain the cell wall.")
            short = questions.add_own(tx, school.teacher, aid, topic_id=topic, kind="short",
                                      prompt="Why do plant cells need a cell wall?", model_answer="It gives support and protection.",
                                      rubric_points=["support", "protection"])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/assignments/{aid}")[1]
            self.assertIn("+ Add your own question", html)
            html = t.post(f"/teacher/assignments/{aid}/own", {"kind": "mcq", "topic_id": str(topic),
                                                             "prompt": "Which of these cells has a cell wall?",
                                                             "options[]": ["Cheek cell", "Onion peel cell", "", ""],
                                                             "answer_index": "1"})[1]
            self.assertIn("Your question was added.", html)
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            qs = questions.assignment_questions(tx, aid)
            own = [q for q in qs if q["origin"] == "teacher"]
            self.assertEqual([q["kind"] for q in own], ["short", "mcq"])
            self.assertTrue(all(q["status"] == "approved" for q in own))
            self.assertEqual(own[1]["options"], ["Cheek cell", "Onion peel cell"])
            self.assertEqual(qs[-1]["id"], own[1]["id"], "added at the end")
            self.assertIn(own[1]["id"], {q["id"] for q in questions.bank(tx)})
            for q in qs:
                if q["status"] == "draft":
                    questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                 expires_at=clock.now() + timedelta(hours=2), targets=[("class", school.class_id)])
            with self.assertRaises(Conflict):
                questions.add_own(tx, school.teacher, aid, topic_id=topic, kind="mcq", prompt="Too late?", options=["a", "b"],
                                  answer_index=0)
            st = school.student(0)
            att = attempts.start_or_resume(tx, st, aid)
            attempts.save_answer(tx, st, att["id"], own[1]["id"], {"index": 1})
            attempts.save_answer(tx, st, att["id"], short, {"text": "The wall gives the plant support and protection."})
        attempts.submit(self.db, self.engine, st, att["id"])
        with self.db.tenant(school.inst) as tx:
            got = {r["question_id"]: float(r["ai_score"] or 0) / float(r["max_score"]) for r in tx.all(
                "SELECT question_id, ai_score, max_score FROM responses WHERE attempt_id=%s", [att["id"]])}
        self.assertEqual(got[own[1]["id"]], 1.0)
        self.assertEqual(got[short], 1.0)


class PracticeFromTheBank(NQTestCase):
    def test_practice_uses_the_schools_approved_questions_first(self):
        """[LN-3][AS-6] A student's practice mission on a topic uses the school's approved multiple-choice questions
        first (written by Claude or a teacher, never 'Basic' ones) from assignments whose results are released; a
        question in an assignment still waiting for its results never appears (no giving away a live task). Each new
        round brings a different mix; when the bank has fewer than five, textbook questions fill the gap. Practice is
        marked instantly and makes no AI call."""
        from novaquest.services import learn

        school = self.make_school(n_students=2)
        done = self.open_assignment(school, kind="quiz", topics=("8.5",), count=10)
        live = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        with self.db.tenant(school.inst) as tx:
            tx.exec("UPDATE questions SET written_by='claude' WHERE kind='mcq'")
            basic = tx.val("""SELECT q.id FROM questions q JOIN assignment_questions aq ON aq.question_id=q.id
                              WHERE aq.assignment_id=%s ORDER BY aq.ord LIMIT 1""", [done])
            tx.exec("UPDATE questions SET written_by='offline' WHERE id=%s", [basic])
            tx.exec("UPDATE assignments SET released_at=now(), state='reviewed' WHERE id=%s", [done])
            def qa(sql, args):  # (question, its options) identifies a question: the basic writer reuses wordings
                return {(r["prompt"], json.dumps(r["options"])) for r in tx.all(sql, args)}
            released = qa("""SELECT q.prompt, q.options FROM questions q JOIN assignment_questions aq
                             ON aq.question_id=q.id WHERE aq.assignment_id=%s AND q.id<>%s""", [done, basic])
            hidden = qa("""SELECT q.prompt, q.options FROM questions q JOIN assignment_questions aq
                           ON aq.question_id=q.id WHERE aq.assignment_id=%s""", [live]) - released
            basic_q = qa("SELECT prompt, options FROM questions WHERE id=%s", [basic]).pop()
            st = school.student(0)
            first = learn.practice(tx, st, school.topics["8.5"])
            prompts = [(q["prompt"], json.dumps(q["options"])) for q in first["questions"]]
            self.assertEqual(len(prompts), 5)
            self.assertTrue(set(prompts) <= released, "all five from the released, approved questions")
            self.assertFalse(set(prompts) & hidden, "nothing from the assignment still open")
            self.assertNotIn(basic_q, prompts, "no Basic questions")
            key = {(r["prompt"], json.dumps(r["options"])): r["answer"]["index"] for r in tx.all(
                "SELECT prompt, options, answer FROM questions WHERE topic_id=%s AND kind='mcq'", [school.topics["8.5"]])}
            res = learn.practice_submit(tx, st, school.topics["8.5"], {i: key[p] for i, p in enumerate(prompts)})
            self.assertEqual(res["score"], 1.0)
            second = [(q["prompt"], json.dumps(q["options"])) for q in learn.practice(tx, st, school.topics["8.5"])["questions"]]
            self.assertNotEqual(second, prompts, "the next round is a different mix")
            # a topic with no released questions: textbook questions as before
            other = learn.practice(tx, st, school.topics["8.3"])["questions"]
            self.assertGreaterEqual(len(other), 3, "textbook questions when the bank has none")


class NeedsToImprove(NQTestCase):
    def test_topics_to_improve_per_student_per_subject(self):
        """[DB-2][DB-5] A student's page lists, for each subject, the topics they most need to work on, worst first,
        with the reasons: a weak score on the topic's questions in an assignment ('Scored 0% in Test task'), a weak last
        practice, a mix-up in their answers, or a topic the class had work on that they never started. Mastered topics
        and strong ones are left out. A button sets that student a revision task on the top topics; the parent report's
        'Worth revising together' shows the same topics with the reason."""
        from novaquest.services import dashboards, mastery
        from tests.live import Client, LiveServer

        school = self.make_school(n_students=2)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=4)
        self.open_assignment(school, kind="quiz", topics=("8.2",), count=2)
        weak, strong = school.student(0), school.student(1)
        for st, good in ((weak, False), (strong, True)):
            attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid, good=good))
        with self.db.tenant(school.inst) as tx:
            mastery.record(tx, weak.user_id, school.topics["8.4"], "practice", 0.4)
            mis = tx.one("SELECT code, label, topic_id FROM misconceptions WHERE topic_id=%s LIMIT 1", [school.topics["8.5"]])
            tx.exec("""UPDATE responses SET misconception_codes=ARRAY[%s] WHERE attempt_id IN
                       (SELECT id FROM attempts WHERE student_id=%s)""", [mis["code"], weak.user_id])
            needs = dashboards.needs_improvement(tx, weak.user_id, school.class_id)
            self.assertEqual(len(needs), 1, "one subject")
            items = {t["code"]: t for t in needs[0]["topics"]}
            self.assertTrue({"8.3", "8.5", "8.4", "8.2"} <= set(items), items.keys())
            self.assertIn("Scored 0% in Test task", items["8.3"]["reasons"])
            self.assertIn("Last practice: 40%", items["8.4"]["reasons"])
            self.assertTrue(any(r.startswith(f"Mixed up: {mis['label']}") for r in items["8.5"]["reasons"]))
            self.assertEqual(items["8.2"]["reasons"], ["Not started yet: the class has had work on it"])
            codes = [t["code"] for t in needs[0]["topics"]]
            self.assertEqual(codes[-1], "8.2", "a topic not yet started comes after weak scores")
            ok = {t["code"] for t in dashboards.needs_improvement(tx, strong.user_id, school.class_id)[0]["topics"]}
            self.assertNotIn("8.3", ok, "a strong result isn't a need")
            tx.exec("""INSERT INTO mastery(institution_id, student_id, topic_id, level, updated_at) VALUES (%s,%s,%s,3,now())
                       ON CONFLICT (student_id, topic_id) DO UPDATE SET level=3""", [school.inst, weak.user_id, school.topics["8.4"]])
            mastery.for_student.cache_clear() if hasattr(mastery.for_student, "cache_clear") else None
            self.assertNotIn("8.4", {t["code"] for t in dashboards.needs_improvement(tx, weak.user_id, school.class_id)[0]["topics"]},
                             "a Mastered topic is left out")
            ov = reports._overview_for(tx, weak.user_id)[1]
            self.assertTrue(ov["plan"] and ov["plan"][0]["why"].startswith(("Scored", "Mixed up")))
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/students/{weak.user_id}")[1]
            self.assertIn("Needs to improve", html)
            self.assertIn("Scored 0% in Test task", html)
            link = re.search(r'href="(/teacher/assignments/new\?[^"]+)"[^>]*>Set Student a revision task', html).group(1)
            self.assertIn(f"students={weak.user_id}", link.replace("&amp;", "&"))
            status, html, _, _ = t.get(link.replace("&amp;", "&"))
            self.assertEqual(status, 200)
        finally:
            live.stop()


class CleanerTextAndQuestions(NQTestCase):
    def test_diagram_labels_stay_out_of_the_text(self):
        """[SY-5][AS-2] A diagram's labels printed beside a paragraph ('Electric lamp', 'Electrode 1', 'container') are
        taken out of the running text, so a sentence no longer reads 'a chemical reaction between the Electrode 1 rods'.
        Headings, and titles like 'and Mixtures' on a second line, are kept. Runs on the real Curiosity chapter when
        present."""
        from novaquest.services import ingest

        page = "\n".join(["Let us start with one of the earliest types of electric cells ever made.", "Electric", "lamp", "",
                          "4.3.1 Voltaic cell", "A Voltaic cell, also known as Galvanic cell, is", "Electric",
                          "shown in Fig. 4.7. It contains two metal rods", "current",
                          "made of different materials and a liquid called an",
                          "electrolyte, placed in a glass or plastic container.", "container",
                          "the electrolyte, which is usually a weak acid or", "Electrode 2",
                          "salt solution. A chemical reaction between the Electrode 1",
                          "rods and the electrolyte produces electricity.", "working. It is then called ‘dead’ and cannot",
                          "Fig. 4.7: Simple representation of", "supply any more electricity.", "Nature of Matter: Elements,",
                          "Compounds,", "and Mixtures"])
        out = ingest.strip_figure_labels(page)
        joined = " ".join(out.split())
        self.assertIn("A chemical reaction between the rods and the electrolyte produces electricity.", joined)
        self.assertIn("Galvanic cell, is shown in Fig. 4.7. It contains two metal rods made of", joined)
        self.assertIn("and cannot supply any more electricity.", joined)
        for gone in ("lamp", "container container", "Electrode 1", "Electrode 2 salt"):
            self.assertNotIn(gone, joined)
        for kept in ("4.3.1 Voltaic cell", "and Mixtures", "Let us start"):
            self.assertIn(kept, out)
        from tests.base import ROOT
        real = ROOT / "data" / "source" / "curiosity" / "hecu104.pdf"
        if real.exists():
            text = " ".join(" ".join(ingest.extract_pages(real)).split())
            self.assertIn("A chemical reaction between the rods and the electrolyte produces electricity.", text)

    def test_topics_titled_as_questions_read_properly(self):
        """[AS-2] A topic titled as a question ('How Does a Battery Generate Electricity?') is asked first, so the
        basic writer never writes 'Which statement about how does a battery generate electricity is correct?'."""
        from novaquest.ai.offline import _which

        self.assertEqual(_which("How Does a Battery Generate Electricity?"),
                         "How Does a Battery Generate Electricity? Which statement is correct?")
        self.assertEqual(_which("Do Liquids Conduct Electricity?", negative=True),
                         "Do Liquids Conduct Electricity? Which statement is NOT correct?")
        self.assertEqual(_which("Microorganisms"), "Which statement about microorganisms is correct?")

    def test_practice_uses_approved_quick_checks_before_textbook_questions(self):
        """[LN-3] When the school's question bank has too few questions on a topic, practice uses the topic's approved
        quick checks and picture-quiz questions (drafted by Claude and checked) before any made from the textbook."""
        from novaquest.services import learn

        school = self.make_school(n_students=1)
        st = school.student(0)
        import psycopg

        from tests.base import OWNER_URL
        tid = school.topics["8.5"]
        with psycopg.connect(OWNER_URL) as conn:  # the shared book's lessons are platform content
            conn.execute("""INSERT INTO lessons(node_id, data, status) VALUES (%s, %s, 'approved')
                            ON CONFLICT (node_id) DO UPDATE SET data=EXCLUDED.data, status='approved'""",
                         [tid, json.dumps({"explain": "x", "checks": [
                             {"q": "Which part controls what enters a cell?", "options": ["Cell membrane", "Cell wall"], "answer": 0},
                             {"q": "Which cells have a cell wall?", "options": ["Animal cells", "Plant cells"], "answer": 1}]})])
            conn.commit()
        with self.db.tenant(school.inst) as tx:
            qs = learn.practice(tx, st, tid)["questions"]
            prompts = [q["prompt"] for q in qs]
            self.assertTrue({"Which part controls what enters a cell?", "Which cells have a cell wall?"} <= set(prompts))
            first = prompts.index("Which cells have a cell wall?")
            res = learn.practice_submit(tx, st, tid, {first: 1})
            self.assertTrue(res["results"][first]["correct"])
