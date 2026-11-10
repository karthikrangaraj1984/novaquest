"""Book pages (LN-11): a topic's real printed pages, rendered from the book's own PDF."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import books, institutions, pages, syllabus
from tests.base import ROOT, NQTestCase, PASSWORD
from tests.test_books import MAP, PDF, chapter_pdfs, made_up_book


def page_text(pdf: Path, index: int) -> str:
    out = subprocess.run(["pdftotext", "-f", str(index), "-l", str(index), str(pdf), "-"], capture_output=True, check=True)
    return " ".join(out.stdout.decode("utf-8", "replace").split())


class BookPages(NQTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-pages-"))
        self.data_dir = self.tmp / "var"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_ncert_topic_pages_render_once_and_only_the_topics_pages(self):
        """[LN-11][NF-PERF] For the NCERT book, a topic's pages (from its passages, gaps filled) are rendered from the
        PDF as JPEGs, cached on disk so a class opening the same page renders it once, and only the topic's own pages
        can be requested."""
        if not PDF.exists() or not pages.renderer():
            self.skipTest("textbook PDF or pdftoppm not present")
        school = self.make_school()
        tid = school.topics["8.5"]
        with self.db.tenant(school.inst) as tx:
            view = pages.page_view(tx, tid)
            self.assertTrue(view["allowed"] and view["ready"])
            nums = view["pages"]
            self.assertTrue(nums and nums == list(range(nums[0], nums[-1] + 1)))
            img = pages.render(tx, tid, nums[0], self.data_dir)
            self.assertEqual(img.read_bytes()[:2], b"\xff\xd8", "a JPEG")
            self.assertLess(img.stat().st_size, 600_000)
            stamp = img.stat().st_mtime_ns
            self.assertEqual(pages.render(tx, tid, nums[0], self.data_dir), img)
            self.assertEqual(img.stat().st_mtime_ns, stamp, "served from the cache, not rendered again")
            for bad in (nums[0] - 1, nums[-1] + 1, 0, 9999):
                with self.assertRaises(NotFound):
                    pages.render(tx, tid, bad, self.data_dir)
        src, index = pages._ncert_source({"chapter_no": "8"}, nums[0])
        self.assertIn("Cell", page_text(src, index), "the printed page number maps to the right PDF page")

    def test_ncert_chapter_files_map_to_the_same_pages(self):
        """[LN-11] When the NCERT book is stored as chapter PDFs (any names), each printed page is found in the right
        chapter file: its text matches the same page of the whole book."""
        if not PDF.exists() or not pages.renderer():
            self.skipTest("textbook PDF or pdftoppm not present")
        src_dir = self.tmp / "source"
        src_dir.mkdir()
        for f in chapter_pdfs(src_dir, (2, 8)):
            f.rename(src_dir / f"download-{f.stem[-1]}x.pdf")
        old = pages.SOURCE_DIR
        pages.SOURCE_DIR = src_dir
        try:
            for chapter, page in (("2", 20), ("8", 95)):
                pdf, index = pages._ncert_source({"chapter_no": chapter}, page)
                self.assertEqual(page_text(pdf, index), page_text(PDF, page + MAP["pdf_page_offset"]))
            self.assertIsNone(pages._ncert_source({"chapter_no": "5"}, 50), "a chapter file that isn't there")
        finally:
            pages.SOURCE_DIR = old

    def test_school_book_pages_need_the_admins_confirmation(self):
        """[LN-11][SY-5][NF-ISO] A school's own book shows no full pages until its admin turns them on and confirms the
        school has the right to show them (recorded in the audit log); then the chapter file's pages render. Another
        school can never get them."""
        if not pages.renderer():
            self.skipTest("pdftoppm not present")
        school = self.make_school()
        other = self.make_school("MOON")
        storage = books.storage_dir(self.data_dir)
        with open(made_up_book(self.tmp / "phy.pdf"), "rb") as fh:
            sid = books.create(self.db, school.admin, board="IGCSE", subject="Physics", grade="8", title="Physics for You",
                               files=[("phy.pdf", fh)], storage=storage)
        from novaquest.ai.offline import OfflineEngine
        books.read_book(self.db, OfflineEngine(), school.inst, sid, storage)
        with self.db.tenant(school.inst) as tx:
            books.publish(tx, school.admin, sid)
            institutions.set_class_subjects(tx, school.admin, school.class_id, [sid])
            tid = syllabus.get_tree(tx, sid)[1]["topics"][0]["id"]
            view = pages.page_view(tx, tid)
            self.assertFalse(view["allowed"])
            with self.assertRaises(NotFound):
                pages.render(tx, tid, view["pages"][0], self.data_dir)
            with self.assertRaises(ValidationError):
                books.set_full_pages(tx, school.admin, sid, True)
            books.set_full_pages(tx, school.admin, sid, True, rights_confirmed=True)
            self.assertTrue(tx.val("SELECT detail->>'rights_confirmed' FROM audit_events WHERE action='book.full_pages'") == "true")
            view = pages.page_view(tx, tid)
            self.assertTrue(view["ready"])
            img = pages.render(tx, tid, view["pages"][0], self.data_dir)
            self.assertEqual(img.read_bytes()[:2], b"\xff\xd8")
        with self.db.tenant(other.inst) as tx:
            with self.assertRaises(NotFound):
                pages.render(tx, tid, view["pages"][0], self.data_dir)
        with self.db.tenant(school.inst) as tx:
            books.set_full_pages(tx, school.admin, sid, False)
            with self.assertRaises(NotFound):
                pages.render(tx, tid, view["pages"][0], self.data_dir)

    def test_book_pages_over_http(self):
        """[LN-11] Over HTTP: a topic page links to its Book pages; the pages screen shows one image per page with a
        text alternative; images are JPEGs, cached privately; teachers can open the same pages from the syllabus board;
        a locked topic's pages can't be opened."""
        if not PDF.exists() or not pages.renderer():
            self.skipTest("textbook PDF or pdftoppm not present")
        from tests.live import Client, LiveServer

        live = LiveServer(self.settings, self.db)
        try:
            school = self.make_school()
            tid = school.topics["8.5"]
            st = Client(live.url)
            st.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = st.get(f"/student/learn/{tid}")[1]
            self.assertIn(f'href="/student/learn/{tid}/pages"', html)
            self.assertIn("Book pages", html)
            status, html, _, _ = st.get(f"/student/learn/{tid}/pages")
            self.assertEqual(status, 200)
            import re
            srcs = re.findall(r'<img src="([^"]+\.jpg)" alt="Page (\d+) of', html)
            self.assertTrue(srcs)
            status, _, headers, _ = st.get(srcs[0][0])
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertIn("private", headers["Cache-Control"])
            self.assertEqual(st.last_bytes[:2], b"\xff\xd8")
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            self.assertIn(f"/teacher/syllabus/topics/{tid}/pages", t.get(f"/teacher/syllabus?topic={tid}")[1])
            self.assertEqual(t.get(f"/teacher/syllabus/topics/{tid}/pages/{srcs[0][1]}.jpg")[0], 200)
            t.post(f"/teacher/topics/{tid}/open", {"class_id": school.class_id, "open": "0"})
            self.assertEqual(st.get(srcs[0][0])[0], 404, "a locked topic's pages are closed too")
        finally:
            live.stop()
            shutil.rmtree(self.settings.data_dir / "pagecache", ignore_errors=True)
