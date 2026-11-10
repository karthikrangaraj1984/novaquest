"""1.27.0: chapter presentations: a teacher teaches a chapter from the projector inside NovaQuest, one period at a
time, with a presenter view; the slides can't be downloaded, printed or saved from the app."""
from __future__ import annotations

from pathlib import Path

from novaquest.services import prep, present
from tests.base import PASSWORD, NQTestCase


class ChapterSlides(NQTestCase):
    def _setup(self):
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            ch = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])
            data, _ = prep.pack(tx, ch)
        return school, ch, data

    def test_slides_follow_the_lesson_plan(self):
        """[PD-1] Each period of the chapter's lesson plan becomes slides: a cover, then for each topic the opening
        question (the teacher's hook instructions go in the notes), the key points one per slide, the approved
        picture-lesson cards, the textbook's own pages, the book's activity with its safety note, a check whose answer
        is revealed on a click, and 'what we learnt'. The activities period shows the book's activities; the review
        period asks 'True or false?' about each mix-up. Every slide has speaker notes and a step of the period's
        timed flow."""
        school, ch, data = self._setup()
        n = len(data["topics"])
        lessons = prep.arrange(data, n + 2, 40)
        with self.db.tenant(school.inst) as tx:
            first = present.slides(tx, ch, data, lessons[0])
            kinds = [s["kind"] for s in first]
            self.assertEqual(kinds[0], "cover")
            self.assertEqual(kinds[1], "hook")
            self.assertTrue(first[1]["text"].endswith("?"), "the class sees a question, not the teacher's instructions")
            self.assertIn(data["topics"][0]["hook"], first[1]["notes"])
            self.assertEqual(kinds.count("point"), len(data["topics"][0]["key_points"]))
            self.assertIn("page", kinds, "the shared NCERT book's pages are shown")
            self.assertIn("check", kinds)
            self.assertEqual(kinds[-2:], ["wrap", "end"])
            self.assertTrue(all(s["notes"] and s["step"]["minutes"] > 0 and s["label"] for s in first))
            self.assertEqual([s["no"] for s in first], list(range(1, len(first) + 1)))
            review = next(p for p in lessons if p["kind"] == "review")
            rs = present.slides(tx, ch, data, review)
            mix = [s for s in rs if s["kind"] == "mixup"]
            self.assertTrue(mix and mix[0]["title"] == "True or false?")
            if data["activities"]:
                act = next(p for p in lessons if p["kind"] == "activity")
                self.assertIn("activity", [s["kind"] for s in present.slides(tx, ch, data, act)])

    def test_present_in_the_app_only(self):
        """[PD-2][UX-1] 'Present on the projector' (and 'Present' on each period) opens the slides full screen in the
        app, with Back/Next, clicker and arrow keys, 'Show answer', a period picker and a presenter view (notes, the
        period's timer, the next slide) that stays in step with the projector. There is no download, the browser is
        told not to keep or index the page and not to print it, saving and copying are turned off, and every slide
        has a watermark with the school, the teacher and the date. Each time a deck is opened it is recorded.
        Textbook page images are only served for the chapter's own topics, and never kept by the browser. A teacher
        who doesn't teach the subject can't open it."""
        from tests.live import Client, LiveServer

        school, ch, data = self._setup()
        stranger = self.staff(school.inst, "Mr Other", "other@sun.test", ["teacher"])
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            prep_page = t.get(f"/teacher/prep?chapter={ch}")[1]
            self.assertIn(f"/teacher/present/{ch}?", prep_page)
            self.assertIn(">Present", prep_page)
            status, html, headers, _ = t.get(f"/teacher/present/{ch}?period=1&format=pdf")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("no-store", headers.get("Cache-Control", ""))
            self.assertIn("noindex", headers.get("X-Robots-Tag", ""))
            self.assertIn("data-dslide", html)
            self.assertIn("/static/deck.js", html)
            self.assertIn("SUN School · Mrs Rao · ", html)
            for word in ("download", "format=pdf", ".pptx"):
                self.assertNotIn(word, html.lower())
            self.assertIn("data-open-presenter", html)
            pres = t.get(f"/teacher/present/{ch}?period=1&view=presenter")[1]
            self.assertIn("data-clock", pres)
            self.assertIn("data-note-now", pres)
            page = next(s for s in present_slides(self, school, ch, data) if s["kind"] == "page")
            st, _, h, _ = t.get(f"/teacher/present/{ch}/page/{page['topic_id']}/{page['page']}.jpg")
            if st == 200:  # the shared book's PDF is on this computer
                self.assertIn("no-store", h.get("Cache-Control", ""))
            other_chapter = page["topic_id"]
            with self.db.tenant(school.inst) as tx:
                elsewhere = tx.val("""SELECT id FROM syllabus_nodes WHERE kind='topic' AND parent_id<>%s AND syllabus_id=%s LIMIT 1""",
                                   [ch, school.syllabus_id])
                self.assertEqual(tx.val("SELECT count(*) FROM audit_events WHERE action='present.open'"), 1)
            self.assertEqual(t.get(f"/teacher/present/{ch}/page/{elsewhere}/{page['page']}.jpg")[0], 404)
            self.assertTrue(other_chapter)
            o = Client(live.url)
            o.login("other@sun.test", PASSWORD)
            self.assertEqual(o.get(f"/teacher/present/{ch}?period=1")[0], 404)
            p = Client(live.url)
            p.login("principal@sun.test", PASSWORD)
            self.assertEqual(p.get(f"/teacher/present/{ch}?period=1&class={school.class_id}")[0], 200)
        finally:
            live.stop()
        self.assertTrue(stranger)
        js = (Path(__file__).parents[1] / "novaquest/web/static/deck.js").read_text()
        for guard in ("contextmenu", "dragstart", "copy", "BroadcastChannel", 'k === "s"', 'k === "p"'):
            self.assertIn(guard, js)
        css = (Path(__file__).parents[1] / "novaquest/web/static/app.css").read_text()
        self.assertIn("@media print { .deckshow > * { display: none !important; }", css)


def present_slides(case, school, ch, data):
    with case.db.tenant(school.inst) as tx:
        return present.slides(tx, ch, data, prep.arrange(data, len(data["topics"]), 40)[0])
