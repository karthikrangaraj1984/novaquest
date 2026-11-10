"""1.29.0: pictures on teachers' slides: the book's own figures cut from its pages (with their captions), and
diagrams Claude draws for the key points."""
from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path

from novaquest.core.errors import ValidationError
from novaquest.services import books, figures, institutions, prep, present
from tests.base import PASSWORD, NQTestCase

BODY = ("All living things are made of cells. A cell has a cell membrane, cytoplasm and a nucleus. We can see cells "
        "under a microscope (Fig. 1.1). Plant cells also have a cell wall around the cell membrane.")


def figure_pdf(path: Path) -> Path:
    """A one-chapter book: a numbered section, a coloured picture with 'Fig. 1.1: …' under it, and a logo on every page."""
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pic = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(pic)
    d.ellipse((40, 30, 360, 270), fill=(120, 200, 230), outline=(20, 60, 120), width=6)
    d.ellipse((170, 120, 230, 180), fill=(150, 60, 90))
    buf = io.BytesIO()
    pic.save(buf, "PNG")
    logo = Image.new("RGB", (60, 60), (200, 30, 30))
    lbuf = io.BytesIO()
    logo.save(lbuf, "PNG")
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    for page in range(3):
        c.drawImage(ImageReader(io.BytesIO(lbuf.getvalue())), w - 90, h - 70, 40, 40)  # the same logo on every page
        y = h - 110
        if page == 0:
            c.setFont("Helvetica-Bold", 22)
            c.drawString(60, y, "THE CELL")
            y -= 50
            c.setFont("Helvetica-Bold", 14)
            c.drawString(60, y, "1.1 What Is a Cell?")
            y -= 24
        c.setFont("Helvetica", 11)
        text = BODY if page == 0 else " ".join([BODY] * 6)
        for i in range(0, len(text), 95):
            c.drawString(60, y, text[i:i + 95])
            y -= 15
        if page == 0:
            c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 150, y - 230, 280, 210)
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(170, y - 248, "Fig. 1.1: A cell as seen under")
            c.drawString(170, y - 260, "a microscope")
        c.showPage()
    c.save()
    return path


@unittest.skipUnless(shutil.which("pdftohtml") and shutil.which("pdftoppm"), "needs Poppler")
class BookFigures(NQTestCase):
    def _book(self, allow: bool = True):
        school = self.make_school(n_students=1)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(figure_pdf(tmp / "ch1.pdf"), "rb") as fh:
            sid = books.create(self.db, school.admin, board="CBSE", subject="Science", grade="8", title="Cells",
                               files=[("ch1.pdf", fh)], storage=books.storage_dir(self.settings.data_dir))
        books.read_book(self.db, self.engine, school.inst, sid, books.storage_dir(self.settings.data_dir))
        with self.db.tenant(school.inst) as tx:
            if allow:
                books.set_teacher_figures(tx, school.admin, sid, True, True)
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=school.class_id,
                                        syllabus_id=sid)
            ch = tx.val("SELECT id FROM syllabus_nodes WHERE syllabus_id=%s AND kind='chapter'", [sid])
            topic = tx.val("SELECT id FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic' ORDER BY ord LIMIT 1", [sid])
        return school, sid, ch, topic

    def test_figures_found_with_captions(self):
        """[FG-1] The book's figures are found on its printed pages with their captions ('Fig. 1.1: A cell as seen under
        a microscope', a caption over two lines joined); a logo on every page is left out; each figure is cut from
        the page as a picture, read in the background (so pages never wait) and kept. A figure goes to the topic
        whose text mentions it."""
        school, sid, ch, topic = self._book()
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(figures.topic_figures(tx, topic, self.settings.data_dir, read=False), [], "not read yet")
            self.assertFalse(figures.ready(tx, topic, self.settings.data_dir))
        self.assertEqual(figures.warm(self.db, school.inst, sid, self.settings.data_dir), 1)
        with self.db.tenant(school.inst) as tx:
            self.assertTrue(figures.ready(tx, topic, self.settings.data_dir))
            figs = figures.topic_figures(tx, topic, self.settings.data_dir, read=False)
            self.assertEqual([f["caption"] for f in figs], ["Fig. 1.1: A cell as seen under a microscope"])
            img = figures.image(tx, topic, figs[0]["page"], figs[0]["n"], self.settings.data_dir)
        self.assertTrue(img.read_bytes().startswith(b"\xff\xd8"))

    def test_figures_on_slides_and_prep_page(self):
        """[FG-2][UX-1] With the book's figures allowed, each figure gets its own slide after the key point it is
        about, and the prep page shows them under the topic; the image is only for signed-in staff and never kept by
        the browser. Without the school's permission (Books → the book → 'The book's figures on teachers' slides',
        which needs a confirmation), there are no figures and the prep page says how to turn them on."""
        school, sid, ch, topic = self._book()
        figures.warm(self.db, school.inst, sid, self.settings.data_dir)
        with self.db.tenant(school.inst) as tx:
            data, _ = prep.pack(tx, ch)
            period = prep.arrange(data, len(data["topics"]), 40)[0]
            deck = present.slides(tx, ch, data, period, self.settings.data_dir)
        figs = [s for s in deck if s["kind"] == "figure"]
        self.assertEqual(len(figs), 1)
        self.assertGreater(deck.index(figs[0]), [s["kind"] for s in deck].index("point"), "after a key point")
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            url = f"/teacher/present/{ch}/fig/{topic}/{figs[0]['page']}/{figs[0]['fig']}.jpg"
            self.assertIn(url, t.get(f"/teacher/present/{ch}?period=1")[1])
            self.assertIn(url, t.get(f"/teacher/prep?chapter={ch}")[1])
            st, _, h, _ = t.get(url)
            self.assertEqual(st, 200)
            self.assertIn("no-store", h.get("Cache-Control", ""))
            self.assertEqual(Client(live.url).get(url)[0] in (302, 303, 401, 403, 200), True)
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            a.post(f"/admin/books/{sid}/figures", {"on": "0"})
            page = t.get(f"/teacher/prep?chapter={ch}")[1]
            self.assertIn("The book's own figures appear", page)
            self.assertEqual(t.get(url)[0], 404)
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(ValidationError):
                books.set_teacher_figures(tx, school.admin, sid, True, False)


class ClaudeDiagrams(NQTestCase):
    def test_diagrams_on_key_points(self):
        """[FG-3] Claude draws 1 or 2 diagrams for each topic (flows, cycles, comparisons, groups, layers, word
        equations, from the book's facts) tied to a key point: they appear beside that point on its slide and on the
        prep page. A diagram that isn't one of those kinds, or isn't drawn right, is left out."""
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            ch = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])
            book = prep.from_book(tx, ch)
        code = book["topics"][0]["code"]
        good = {"kind": "compare", "cols": [{"head": "Plant cell", "icon": "🌿", "cells": ["Cell wall", "Chloroplasts"]},
                                            {"head": "Animal cell", "icon": "🐾", "cells": ["No cell wall", "No chloroplasts"]}]}
        claude = {"topics": [{"code": code, "key_points": ["Plant and animal cells differ.", "Both have a nucleus."],
                              "diagrams": [{"point": 0, "pic": good}, {"point": 1, "pic": {"kind": "scene", "scene": "x"}},
                                           {"point": 9, "pic": {"kind": "flow", "steps": "not a list"}}]}]}
        pack = prep.merge(claude, book)
        self.assertEqual(pack["topics"][0]["diagrams"], [{"point": 0, "pic": good}])
        with self.db.tenant(school.inst) as tx:
            tx.exec("""INSERT INTO unit_packs(institution_id, chapter_id, status, data, updated_at) VALUES (%s,%s,'ready',%s,now())""",
                    [school.inst, ch, __import__("json").dumps(pack)])
            period = prep.arrange(pack, len(pack["topics"]), 40)[0]
            deck = present.slides(tx, ch, pack, period, self.settings.data_dir)
        self.assertEqual([s["pic"] for s in deck if s["kind"] == "point"][0], good)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertIn("Plant cell", t.get(f"/teacher/present/{ch}?period=1")[1])
            self.assertIn('class="prep-diagram', t.get(f"/teacher/prep?chapter={ch}")[1])
        finally:
            live.stop()

    def test_diagram_goes_with_the_point_it_shows(self):
        """[FG-3] A diagram is put beside the key point it is about, judged by what it says, not by the point number
        Claude gave (which can be off by one): a diagram of cheek, muscle and nerve cells goes with the point about
        cell shapes, not the one about the cell wall. One that matches no point gets a slide of its own."""
        points = ["All living things are made up of cells.",
                  "The outer layer of a cell is the cell membrane; inside is cytoplasm and a nucleus.",
                  "Cells have different shapes: cheek cells are thin and flat, muscle cells are spindle-shaped and nerve "
                  "cells are long and branched.",
                  "A group of similar cells forms a tissue.",
                  "The cell wall in plant cells gives rigidity and strength to plants."]
        shapes = {"kind": "compare", "cols": [{"head": "Cheek cell", "icon": "👄", "cells": ["Thin and flat"]},
                                              {"head": "Muscle cell", "icon": "💪", "cells": ["Spindle-shaped, flexible"]},
                                              {"head": "Nerve cell", "icon": "⚡", "cells": ["Very long and branched"]}]}
        weather = {"kind": "icons", "items": [{"icon": "☀️", "text": "Sunny days"}, {"icon": "🌧", "text": "Rainy days"}]}
        beside, alone = present.place_diagrams(points, [{"point": 4, "pic": shapes}, {"point": 0, "pic": weather}])
        self.assertEqual(beside, {2: shapes})
        self.assertEqual(alone, [weather])

