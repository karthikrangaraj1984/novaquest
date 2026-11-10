"""1.14.1: uploaded books survive upgrades. Each version unzips into a new folder, and books were kept in that folder's
var\\books, so a new version started without them (Book pages and Read again need the PDF)."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from novaquest.core.config import ROOT, default_data_dir
from novaquest.services import books
from tests.base import PASSWORD, NQTestCase
from tests.test_books import made_up_book


class BooksSurviveUpgrades(NQTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-upg-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_data_folder_is_outside_the_version_folder_on_windows(self):
        """[NF-WIN] On Windows uploaded books are kept in %LOCALAPPDATA%\\NovaQuest\\data (next to the database), not
        in the version folder; NQ_DATA_DIR still wins; elsewhere it stays ./var."""
        local = r"C:\Users\k\AppData\Local"
        self.assertEqual(default_data_dir({"LOCALAPPDATA": local}, windows=True), Path(local) / "NovaQuest" / "data")
        self.assertEqual(default_data_dir({"LOCALAPPDATA": local, "NQ_DATA_DIR": r"D:\nq"}, windows=True), Path(r"D:\nq"))
        self.assertEqual(default_data_dir({}, windows=False), ROOT / "var")

    def test_books_are_copied_across_from_earlier_version_folders(self):
        """[NF-WIN][SY-5] `nq up` copies each uploaded book (its PDFs and saved page text) that isn't in the data folder
        yet out of earlier NovaQuest folders next to this one (Downloads\\novaquest-1.13.1\\novaquest-1.13.1\\var\\
        books, or one level less deep), newest copy first, never overwriting and leaving the old folders as they are.
        `nq status` and the book's page say when a book's file is missing."""
        downloads = self.tmp / "Downloads"
        old = downloads / "novaquest-1.13.1" / "novaquest-1.13.1" / "var" / "books" / "3" / "7"
        older = downloads / "novaquest-1.12.0" / "var" / "books" / "3"
        (old / "a.pdf.ocr").mkdir(parents=True)
        (old / "a.pdf").write_bytes(b"%PDF new")
        (old / "a.pdf.ocr" / "0001.txt").write_text("page one")
        for sid, body in (("7", b"%PDF stale"), ("9", b"%PDF other")):
            (older / sid).mkdir(parents=True)
            (older / sid / "a.pdf").write_bytes(body)
        past = time.time() - 3600
        os.utime(downloads / "novaquest-1.12.0" / "var" / "books", (past, past))
        root = downloads / "novaquest-1.14.1" / "novaquest-1.14.1"
        root.mkdir(parents=True)
        storage = self.tmp / "AppData" / "NovaQuest" / "data" / "books"
        self.assertEqual(sorted(books.bring_books_across(storage, root)), ["3/7", "3/9"])
        self.assertEqual((storage / "3" / "7" / "a.pdf").read_bytes(), b"%PDF new", "the newest copy wins")
        self.assertEqual((storage / "3" / "7" / "a.pdf.ocr" / "0001.txt").read_text(), "page one")
        self.assertTrue((old / "a.pdf").exists(), "the old folder is left as it was")
        (storage / "3" / "7" / "a.pdf").write_bytes(b"%PDF edited")
        self.assertEqual(books.bring_books_across(storage, root), [], "nothing twice, nothing overwritten")
        self.assertEqual((storage / "3" / "7" / "a.pdf").read_bytes(), b"%PDF edited")

        from novaquest.ai.offline import OfflineEngine
        from tests.live import Client, LiveServer

        school = self.make_school()
        store = books.storage_dir(self.settings.data_dir)
        pdf = made_up_book(self.tmp / "physics.pdf")
        with pdf.open("rb") as fh:
            sid = books.create(self.db, school.admin, board="IGCSE", subject="Science", grade="8", title="Physics for You",
                               files=[(pdf.name, fh)], storage=store)
        books.read_book(self.db, OfflineEngine(), school.inst, sid, store)
        self.assertEqual(books.missing_files(self.db, store), [])
        shutil.rmtree(store / str(school.inst) / str(sid))
        self.assertEqual([m["title"] for m in books.missing_files(self.db, store)], ["Physics for You"])
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("Not on this computer", html)
            self.assertIn("nq up", html)
        finally:
            live.stop()
        src = (ROOT / "tools" / "nq.py").read_text()
        self.assertIn("_keep_books()", src)
        self.assertIn("_book_files_status()", src)


class RefreshBookText(NQTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="nq-refresh-"))
        self.storage = self.tmp / "books"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _book(self, school, files, title):
        from novaquest.ai.offline import OfflineEngine

        handles = [(p.name, p.open("rb")) for p in files]
        try:
            sid = books.create(self.db, school.admin, board="CBSE", subject="Science", grade="8", title=title,
                               files=handles, storage=self.storage)
        finally:
            for _, h in handles:
                h.close()
        books.read_book(self.db, OfflineEngine(), school.inst, sid, self.storage)
        return sid

    def test_refresh_replaces_each_topics_text_and_keeps_everything_else(self):
        """[SY-5] 'Refresh the book's text' reads the stored PDF again and replaces the text under each existing topic,
        keeping topic names, ids, lessons and progress. A chapter whose topics can't be found in the new text (a topic
        renamed so its heading no longer matches) is left as it was and listed. A missing PDF is reported plainly."""
        from novaquest.services import syllabus

        school = self.make_school()
        sid = self._book(school, [made_up_book(self.tmp / "physics.pdf")], "Physics for You")
        with self.db.tenant(school.inst) as tx:
            tree = syllabus.get_tree(tx, sid)
            ids = [t["id"] for c in tree for t in c["topics"]]
            tx.exec("UPDATE source_passages SET body = 'Electrode 1 OLD TEXT' WHERE syllabus_id=%s", [sid])
            books.rename(tx, school.admin, sid, tree[1]["topics"][1]["id"], "Wheels and Ropes")  # chapter 2 no longer matches
            books.request_refresh(tx, school.admin, sid)
        state = books.refresh_text(self.db, school.inst, sid, self.storage)
        self.assertEqual((state["stage"], state["chapters"], state["skipped"]), ("done", 1, ["2"]))
        with self.db.tenant(school.inst) as tx:
            self.assertEqual([t["id"] for c in syllabus.get_tree(tx, sid) for t in c["topics"]], ids, "same topics")
            ch1 = [t["id"] for t in tree[0]["topics"]]
            bodies = {r["node_id"]: r["body"] for r in tx.all("SELECT node_id, body FROM source_passages WHERE syllabus_id=%s", [sid])}
            self.assertTrue(all("OLD TEXT" not in bodies[i] for i in ch1), "chapter 1's text is fresh")
            self.assertIn("Distance is the total length", " ".join(bodies[i] for i in ch1))
            self.assertTrue(all(bodies[t["id"]] == "Electrode 1 OLD TEXT" for t in tree[1]["topics"]), "chapter 2 untouched")
            self.assertEqual(books.detail(tx, sid)["book"]["meta"]["refresh"]["stage"], "done")
        shutil.rmtree(self.storage / str(school.inst) / str(sid))
        state = books.refresh_text(self.db, school.inst, sid, self.storage)
        self.assertEqual(state["stage"], "failed")
        self.assertIn("isn't on this computer", state["error"])

    def test_refresh_over_http_and_on_the_real_curiosity_chapter(self):
        """[SY-5][UX-1] Over HTTP the book page has 'Refresh the book's text'; the worker refreshes it and the page
        says how many chapters were refreshed. On NCERT's Curiosity chapter 4 (when present) the refreshed text says
        'A chemical reaction between the rods and the electrolyte produces electricity', without the diagram label."""
        from novaquest import worker
        from tests.base import ROOT
        from tests.live import Client, LiveServer

        real = ROOT / "data" / "source" / "curiosity" / "hecu104.pdf"
        self.storage = books.storage_dir(self.settings.data_dir)  # where the worker looks
        self.addCleanup(shutil.rmtree, self.storage, True)
        school = self.make_school()
        sid = self._book(school, [real if real.exists() else made_up_book(self.tmp / "p.pdf")], "Curiosity 8")
        if real.exists():
            with self.db.tenant(school.inst) as tx:
                tx.exec("""UPDATE source_passages SET body = replace(body, 'between the rods', 'between the Electrode 1 rods')
                           WHERE syllabus_id=%s""", [sid])
        live = LiveServer(self.settings, self.db)
        try:
            a = Client(live.url)
            a.login("admin@sun.test", PASSWORD)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("Refresh the book's text", html)
            a.post(f"/admin/books/{sid}/refresh")
            worker.run_due(self.db, self.settings)
            html = a.get(f"/admin/books/{sid}")[1]
            self.assertIn("Text refreshed", html)
        finally:
            live.stop()
        if real.exists():
            with self.db.tenant(school.inst) as tx:
                text = " ".join(r["body"] for r in tx.all("SELECT body FROM source_passages WHERE syllabus_id=%s", [sid]))
            self.assertIn("A chemical reaction between the rods and the electrolyte produces electricity", text)
            self.assertNotIn("Electrode 1 rods", text)
