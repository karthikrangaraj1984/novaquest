"""Learn in pictures: a picture-first lesson for every topic (LN-10)."""
from __future__ import annotations

import copy
import json
import re

from novaquest.core.errors import Forbidden, NotFound
from novaquest.services import learn, mastery, pictures, syllabus
from novaquest.web.pictures import picture
from tests.base import ROOT, PASSWORD, NQTestCase
from tests.live import Client, LiveServer


class PictureContent(NQTestCase):
    def test_every_topic_has_a_valid_picture_lesson(self):
        """[LN-10] Every one of the 125 syllabus topics has a picture lesson that passes validation: 3 to 8 picture cards
        in short sentences, the crux in 2 to 4 points and a 2 to 4 question picture quiz with valid answers."""
        data = json.loads((ROOT / "data" / "syllabus" / "ncert_g8_science.json").read_text())
        codes = {t["sec"] for c in data["chapters"] for t in c["topics"]}
        lessons = pictures.all_lessons()
        self.assertEqual(len(codes), 125)
        self.assertEqual(set(lessons), codes, "a picture lesson for every topic, and none for unknown topics")
        cards = 0
        for code, lesson in lessons.items():
            self.assertTrue(3 <= len(lesson["cards"]) <= 8, code)
            self.assertTrue(2 <= len(lesson["quiz"]) <= 4, code)
            cards += len(lesson["cards"])
            for c in lesson["cards"]:
                self.assertIn("pic", c, code)
                for s in c["say"]:
                    self.assertLessEqual(len(s), pictures.MAX_SENTENCE, f"{code}: keep sentences short")
        self.assertGreaterEqual(cards, 400)

    def test_every_drawing_is_used_and_labelled(self):
        """[LN-10] Every original drawing has named label points, is used by at least one lesson, and every label
        and '?' marker points at a real part of its drawing."""
        anchors = pictures.scene_anchors()
        self.assertGreaterEqual(len(anchors), 40)
        used = set()

        def walk(pic):
            if pic.get("kind") == "scene":
                used.add(pic["scene"])
                for lab in pic.get("labels", []):
                    self.assertIn(lab["at"], anchors[pic["scene"]])
        for lesson in pictures.all_lessons().values():
            for c in lesson["cards"]:
                walk(c["pic"])
            for q in lesson["quiz"]:
                if "pic" in q:
                    walk(q["pic"])
        self.assertEqual(set(anchors) - used, set(), "drawings no lesson uses")
        for name, pts in anchors.items():
            self.assertTrue(pts, name)
            for x, y in pts.values():
                self.assertTrue(0 <= x <= 400 and 0 <= y <= 300, f"{name}: point off the drawing")

    def test_validation_rejects_broken_content(self):
        """[LN-10] Broken lessons are rejected before any student sees them: unknown drawing or part, wrong quiz
        answer, too few cards, sentences that are too long."""
        good = copy.deepcopy(pictures.all_lessons()["8.5"])
        pictures.validate_lesson("8.5", good)
        cases = []
        bad = copy.deepcopy(good); bad["cards"][0]["pic"] = {"kind": "scene", "scene": "no_such", "labels": [{"at": "x", "name": "X"}]}; cases.append(bad)
        bad = copy.deepcopy(good); bad["cards"][0]["pic"]["labels"][0]["at"] = "spleen"; cases.append(bad)
        bad = copy.deepcopy(good); bad["quiz"][0]["answer"] = 9; cases.append(bad)
        bad = copy.deepcopy(good); bad["cards"] = bad["cards"][:2]; cases.append(bad)
        bad = copy.deepcopy(good); bad["cards"][1]["say"] = ["word " * 80]; cases.append(bad)
        bad = copy.deepcopy(good); bad["cards"][1]["pic"] = {"kind": "hologram"}; cases.append(bad)
        for b in cases:
            with self.assertRaises(pictures.ContentError):
                pictures.validate_lesson("8.5", b)

    def test_pictures_render_accessibly_and_safely(self):
        """[LN-10][NF-A11Y] Drawings carry a text alternative and a numbered legend matching the markers; every kind
        renders; text is escaped."""
        html = str(picture({"kind": "scene", "scene": "animal_cell",
                            "labels": [{"at": "nucleus", "name": "Nucleus <script>", "note": "Control & centre"}]}, "t"))
        self.assertIn('role="img"', html)
        self.assertIn('aria-label="', html)
        self.assertEqual(html.count('class="mk"'), 1)
        self.assertEqual(html.count('class="lg"'), 1)
        self.assertIn("Nucleus &lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("data-hide-names", html)
        ask = str(picture({"kind": "scene", "scene": "animal_cell", "ask": "nucleus"}, "q"))
        self.assertIn('class="mk ask"', ask)
        self.assertNotIn("legend", ask)
        kinds = set()
        for lesson in pictures.all_lessons().values():
            for c in lesson["cards"]:
                kinds.add(c["pic"]["kind"])
                self.assertIn("<figure", str(picture(c["pic"], "x")))
        self.assertEqual(kinds, pictures.KINDS, "every picture kind is used and renders")


class PictureLessons(NQTestCase):
    def test_student_deck_quiz_and_finish(self):
        """[LN-10][LN-5] A student opens a topic's picture lesson (quiz answers are not sent to the browser), answers
        the picture quiz (checked on the server, counts towards mastery) and marks the topic finished."""
        school = self.make_school()
        st = school.student(0)
        tid = school.topics["8.5"]
        with self.db.tenant(school.inst) as tx:
            chapters = pictures.index(tx, st)
            self.assertEqual(sum(len(c["topics"]) for c in chapters), 125)
            deck = pictures.deck(tx, st, tid)
            self.assertEqual(deck["topic"]["code"], "8.5")
            self.assertEqual(deck["next_topic"]["code"], "8.6")
            self.assertFalse(deck["done"])
            for q in deck["lesson"]["quiz"]:
                self.assertNotIn("answer", q)
                self.assertNotIn("why", q)
            wrong = pictures.check(tx, st, tid, 0, 1)
            self.assertFalse(wrong["correct"])
            self.assertIsNone(wrong["answer"])
            right = pictures.check(tx, st, tid, 0, 0)
            self.assertTrue(right["correct"])
            self.assertEqual(right["answer"], "Nucleus")
            self.assertEqual(right["level"], 2)  # a correct check makes the topic "Practised"
            with self.assertRaises(NotFound):
                pictures.check(tx, st, tid, 99, 0)
            self.assertEqual(pictures.finish(tx, st, tid), 2)
            pictures.finish(tx, st, tid)  # finishing twice records it once
            self.assertEqual(tx.val("SELECT count(*) FROM learn_events WHERE student_id=%s AND kind='pictures'", [st.user_id]), 1)
            self.assertTrue(pictures.deck(tx, st, tid)["done"])
            self.assertEqual(pictures.class_progress(tx, school.class_id), {tid: 1})
            last = pictures.deck(tx, st, school.topics["8.6"])
            self.assertIsNone(last["next_topic"], "last topic of the chapter has no next topic")

    def test_locked_topics_and_roles(self):
        """[LN-10][LN-7] Picture lessons follow the teacher's topic locks; teachers and leaders can preview the full
        lesson with answers; students can't open the preview."""
        school = self.make_school()
        tid = school.topics["10.6"]
        with self.db.tenant(school.inst) as tx:
            learn.set_topic_open(tx, school.teacher, school.class_id, tid, False)
            with self.assertRaises(NotFound):
                pictures.deck(tx, school.student(0), tid)
            self.assertNotIn("10.6", {t["code"] for c in pictures.index(tx, school.student(0)) for t in c["topics"]})
            for who in (school.teacher, school.leader):
                pv = pictures.preview(tx, who, tid)
                self.assertIn("answer", pv["lesson"]["quiz"][0])
            with self.assertRaises(Forbidden):
                pictures.preview(tx, school.student(0), tid)


class PictureWeb(NQTestCase):
    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def test_picture_module_over_http(self):
        """[LN-10][NF-A11Y] Over HTTP: the Learn page and every topic page link to Learn in pictures; the module lists
        topics by chapter; a deck page holds every card, the crux and the quiz; the quiz and finish APIs work; the
        teacher's syllabus board links to a preview with answers."""
        school = self.make_school()
        tid = school.topics["8.5"]
        st = Client(self.live.url)
        st.login_student("SUN", "8B", "1", school.students[0]["pin"])
        status, html, _, _ = st.get("/student/learn")
        self.assertIn('href="/student/pictures"', html)
        status, html, _, _ = st.get(f"/student/learn/{tid}")
        self.assertIn(f'href="/student/learn/{tid}/pictures"', html)
        status, html, _, _ = st.get("/student/pictures")
        self.assertEqual(status, 200)
        self.assertEqual(html.count('class="topic-row pic-row"'), 125)
        self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1)
        status, html, _, _ = st.get(f"/student/learn/{tid}/pictures")
        self.assertEqual(status, 200)
        lesson = pictures.all_lessons()["8.5"]
        self.assertEqual(html.count("data-card"), len(lesson["cards"]) + 2)
        self.assertIn("The crux", html)
        self.assertIn(lesson["crux"].split(":")[0], html)
        self.assertNotIn(lesson["quiz"][0]["why"], html, "explanations are only sent after answering")
        self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1)
        status, body, _ = st.post_json(f"/api/learn/{tid}/pictures/check", {"index": 1, "choice": 0})
        self.assertEqual(status, 200)
        self.assertTrue(body["correct"])
        status, body, _ = st.post_json(f"/api/learn/{tid}/pictures/done", {})
        self.assertEqual(status, 200)
        self.assertIn("Done ✓", st.get("/student/pictures")[1])
        status, _, _ = st.post_json(f"/api/learn/{tid}/pictures/done", {}, csrf=False)
        self.assertEqual(status, 403, "state changes need the CSRF token")
        self.assertEqual(st.get(f"/teacher/syllabus/topics/{tid}/pictures")[0], 403)
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        status, html, _, _ = t.get(f"/teacher/syllabus?class={school.class_id}&topic={tid}")
        self.assertIn(f'href="/teacher/syllabus/topics/{tid}/pictures"', html)
        self.assertIn("1 of 6 students finished it", html)
        status, html, _, _ = t.get(f"/teacher/syllabus/topics/{tid}/pictures")
        self.assertEqual(status, 200)
        self.assertIn(lesson["quiz"][0]["why"], html, "teachers see the answers and explanations")
        self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1)
