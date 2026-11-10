"""The whole local stack, started by the launcher: Postgres + pgvector, Mailpit, Caddy (HTTPS), web and
worker. Checks HTTPS, NOTIFY-driven jobs and email delivery (NF-LOCAL, NR-1)."""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path

import psycopg

from tests.base import ADMIN_URL, ROOT


def nq_initdb(pg_bin: str, data: Path) -> list[str]:
    from tools import nq

    return nq.initdb_cmd(pg_bin, str(data), windows=False)

BIN = os.environ.get("NQ_BIN_DIR", "/opt/nqbin")
HAVE_BINS = all(Path(BIN, b).exists() for b in ("mailpit", "caddy"))


@unittest.skipUnless(HAVE_BINS, f"mailpit and caddy binaries not found in {BIN}")
class LocalStack(unittest.TestCase):
    db_name = "nq_ops_test"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="nq-ops-"))
        owner = ADMIN_URL.replace("postgres@", "nq_owner:nq_owner@").rsplit("/", 1)[0] + "/" + cls.db_name
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{cls.db_name}" WITH (FORCE)')
        cls.env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / ".vendor"), str(ROOT)]),
                   "NQ_DATABASE_URL": owner, "NQ_APP_DATABASE_URL": owner.replace("nq_owner:nq_owner", "nq_app:nq_app"),
                   "NQ_ADMIN_DATABASE_URL": ADMIN_URL, "NQ_SECRET_KEY": "ops-secret", "NQ_BIN_DIR": BIN,
                   "NQ_DATA_DIR": str(cls.tmp / "var"), "NQ_WEB_PORT": "18000", "NQ_SITE": "localhost:18443",
                   "NQ_LAN_SITE": "127.0.0.1:18443", "NQ_BASE_URL": "https://localhost:18443", "NQ_TICK_SECONDS": "1",
                   "XDG_DATA_HOME": str(cls.tmp / "caddy-data"), "XDG_CONFIG_HOME": str(cls.tmp / "caddy-config"),
                   "NQ_AI_MODE": "offline"}
        cls.env.pop("ANTHROPIC_API_KEY", None)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run([sys.executable, "-m", "tools.nq", "down"], cwd=ROOT, env=cls.env, capture_output=True, timeout=60)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def nq(self, *args: str, timeout: int = 180) -> str:
        res = subprocess.run([sys.executable, "-m", "tools.nq", *args], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def test_launcher_runs_the_full_local_stack(self):
        """[NF-LOCAL] One command sets up and starts everything locally: Postgres with pgvector, Mailpit, Caddy HTTPS,
        web app and worker; only the AI model would be external. [NR-1] Emails reach the local mail catcher."""
        out = self.nq("init")
        self.assertIn("No school books yet", out, "the retired NCERT 2018 book isn't loaded")
        self.assertNotIn("NCERT", out)
        out = self.nq("seed", "--password", "OpsPass12345")
        self.assertIn("Demo school created", out)
        self.assertIn("School books published: 1.", self.nq("init"), "the demo school uploads its own demo book")
        out = self.nq("up", "--workers", "2")
        self.assertIn("Ready:", out)
        status = self.nq("status")
        for name in ("mailpit", "web", "worker", "caddy"):
            self.assertIn(f"{name:9} running", status)
        root_crt = self.tmp / "caddy-data/caddy/pki/authorities/local/root.crt"
        for _ in range(60):
            if root_crt.exists():
                break
            time.sleep(0.5)
        ctx = ssl.create_default_context(cafile=str(root_crt))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        body = None
        for _ in range(40):
            try:
                with opener.open("https://localhost:18443/healthz", timeout=5) as r:
                    body = json.loads(r.read())
                    self.assertIn("max-age", r.headers["Strict-Transport-Security"])
                    break
            except OSError:
                time.sleep(0.5)
        self.assertEqual(body, {"ok": True})
        req = urllib.request.Request("https://localhost:18443/login/student", headers={"Accept-Encoding": "gzip"})
        with opener.open(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        with psycopg.connect(self.env["NQ_DATABASE_URL"]) as conn:
            self.assertEqual(conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0], "0.8.0")
            conn.execute("""INSERT INTO jobs(kind, institution_id, payload, run_at, dedupe_key)
                            SELECT 'completion_digest', a.institution_id, jsonb_build_object('assignment_id', a.id), now(), 'ops-test'
                            FROM assignments a LIMIT 1""")
            conn.execute("NOTIFY nq_jobs")
            conn.commit()
        mail = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        subjects: list[str] = []
        for _ in range(40):
            with mail.open("http://127.0.0.1:8025/api/v1/messages", timeout=5) as r:
                subjects = [m["Subject"] for m in json.loads(r.read())["messages"]]
            if any("completed" in s for s in subjects):
                break
            time.sleep(0.5)
        self.assertTrue(any("Cell Lab mission" in s and "completed" in s for s in subjects), subjects)
        with psycopg.connect(self.env["NQ_DATABASE_URL"]) as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE dedupe_key='ops-test'").fetchone()[0], "done")
        out = self.nq("down")
        self.assertIn("web       stopped", out)
        self.assertIn("stopped", self.nq("status"))


class SetKey(unittest.TestCase):
    def test_set_key_saves_hidden_key_to_env(self):
        """[NF-MODEL] `nq set-key` stores the Claude API key in .env (owner-only), rejects malformed keys,
        keeps other settings, and never prints the full key."""
        import stat

        tmp = Path(tempfile.mkdtemp(prefix="nq-key-"))
        env_file = tmp / ".env"
        env_file.write_text("NQ_SECRET_KEY=change-me-to-a-long-random-string\nNQ_BASE_URL=https://x:8443\nANTHROPIC_API_KEY=\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / ".vendor"), str(ROOT)])}
        fake = "sk-ant-api03-" + "A" * 40 + "wxyz"

        def run(key: str):
            return subprocess.run([sys.executable, "-m", "tools.nq", "set-key", "--from-stdin", "--env-file", str(env_file)],
                                  cwd=ROOT, env=env, input=key + "\n", capture_output=True, text=True, timeout=60)
        bad = run("novaquest_api_key")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("doesn't look like a Claude API key", bad.stdout + bad.stderr)
        ok = run(fake)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn(fake, ok.stdout)
        self.assertIn("…wxyz", ok.stdout)
        text = env_file.read_text()
        self.assertIn(f"ANTHROPIC_API_KEY={fake}", text)
        self.assertIn("NQ_BASE_URL=https://x:8443", text)
        self.assertNotIn("change-me", text)
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        shutil.rmtree(tmp, ignore_errors=True)


class WindowsSupport(unittest.TestCase):
    """Windows can't be run in the build workspace, so these check the Windows-specific logic directly and build
    the installer; the installer's own unit tests run inside build_installer."""

    def test_launcher_windows_commands(self):
        """[NF-WIN] On Windows the launcher calls pg_ctl.exe/initdb.exe without Unix-only options, finds helper
        programs (NQ_BIN_DIR, NQ_EXTRA_PATH) and checks processes without signalling them."""
        from tools import nq

        cmd = nq.pg_ctl_cmd(r"C:\NQ\pgsql\bin", r"C:\Users\A B\pgdata", "54329", r"C:\log.txt", "start", "-w", windows=True)
        self.assertTrue(cmd[0].endswith("pg_ctl.exe"))
        self.assertEqual(cmd[cmd.index("-o") + 1], "-p 54329 -c listen_addresses=127.0.0.1")
        self.assertEqual(cmd[-2:], ["start", "-w"])
        self.assertIn("-k /data", " ".join(nq.pg_ctl_cmd("/pg", "/data", "1", "/l", windows=False)))
        init = nq.initdb_cmd(r"C:\NQ\pgsql\bin", r"C:\d", windows=True)
        self.assertTrue(init[0].endswith("initdb.exe"))
        self.assertIn("--no-locale", init)
        self.assertNotIn("--locale=C.UTF-8", init)
        self.assertIn("--locale=C.UTF-8", nq.initdb_cmd("/pg", "/d", windows=False))
        tmp = Path(tempfile.mkdtemp(prefix="nq-path-"))
        (tmp / "bin").mkdir()
        (tmp / "poppler").mkdir()
        old = {k: os.environ.get(k) for k in ("PATH", "NQ_BIN_DIR", "NQ_EXTRA_PATH")}
        try:
            os.environ.update(NQ_BIN_DIR=str(tmp / "bin"), NQ_EXTRA_PATH=os.pathsep.join([str(tmp / "poppler"), str(tmp / "nope")]))
            nq._add_bin_dirs_to_path()
            parts = os.environ["PATH"].split(os.pathsep)
            self.assertEqual(parts[:2], [str(tmp / "bin"), str(tmp / "poppler")])
            self.assertNotIn(str(tmp / "nope"), parts)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertTrue(nq._pid_running(os.getpid()))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(nq._pid_running(dead.pid))

    def test_launcher_starts_postgres_safely(self):
        """[NF-WIN][NF-LOCAL] `nq up` on a fresh folder (no var/logs yet) starts the managed Postgres, says when it's
        already running, refuses with a clear 'run nq init first' when the cluster doesn't exist, and when Postgres
        can't start it shows the log and stops instead of starting the web app against no database."""
        pg_bin = "/usr/lib/postgresql/16/bin"
        if not Path(pg_bin, "pg_ctl").exists():
            self.skipTest("PostgreSQL 16 binaries not found")
        import pwd
        import socket

        tmp = Path(tempfile.mkdtemp(prefix="nq-pgstart-"))
        as_user = []
        if os.geteuid() == 0:  # Postgres refuses to run as root: run the launcher as the workspace user
            user = pwd.getpwnam("claude")
            os.chown(tmp, user.pw_uid, user.pw_gid)
            as_user = ["runuser", "-u", "claude", "--"]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        data, var = tmp / "pgdata", tmp / "var"
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / ".vendor"), str(ROOT)]), "NQ_PG_BIN": pg_bin,
               "NQ_PG_DATA": str(data), "NQ_PG_PORT": port, "NQ_DATA_DIR": str(var)}

        def run(code: str):
            return subprocess.run([*as_user, sys.executable, "-c", f"from tools import nq; {code}"], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=120)
        try:
            res = run("nq.cmd_up(None)")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Run  nq init  first", res.stdout + res.stderr)
            self.assertNotIn("web", res.stdout, "nothing else starts without a database")
            subprocess.run([*as_user, *nq_initdb(pg_bin, data)], check=True, capture_output=True, timeout=120)
            self.assertFalse(var.exists())
            res = run("nq._pg_start()")
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("postgres  started", res.stdout)
            self.assertTrue((var / "logs" / "postgres.log").exists(), "the log folder is created first")
            res = run("nq._pg_start(); nq.cmd_status(None)")
            self.assertIn("postgres  already running", res.stdout)
            self.assertIn("postgres  running", res.stdout)
            res = run("nq.cmd_down(None)")
            self.assertIn("postgres  stopped", res.stdout)
            with open(data / "postgresql.conf", "a") as f:
                f.write("\nshared_buffers = 'nonsense'\n")
            res = run("nq._pg_start()")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("postgres  could not start", res.stdout + res.stderr)
            self.assertIn("shared_buffers", res.stdout + res.stderr, "the reason from the log is shown")
            res = run("nq.cmd_down(None)")
            self.assertIn("postgres  was not running", res.stdout)
        finally:
            subprocess.run([*as_user, str(Path(pg_bin, "pg_ctl")), "-D", str(data), "stop", "-m", "immediate"],
                           capture_output=True, timeout=60)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_windows_installer_builds(self):
        """[NF-WIN] The prerequisites installer passes its unit tests (verified downloads, safe unzip, .env setup,
        signature policy, step reporting) and builds as a 64-bit Windows executable."""
        from tools.build_installer import build

        out = build(Path(tempfile.mkdtemp(prefix="nq-exe-")) / "setup.exe")
        data = out.read_bytes()
        self.assertEqual(data[:2], b"MZ")
        pe = int.from_bytes(data[0x3C:0x40], "little")
        self.assertEqual(data[pe:pe + 4], b"PE\0\0")
        self.assertEqual(int.from_bytes(data[pe + 4:pe + 6], "little"), 0x8664)  # x86-64
        self.assertLess(len(data), 20 * 1024 * 1024)
        shutil.rmtree(out.parent, ignore_errors=True)

    def test_init_adds_textbook_loaded_later(self):
        """[SY-1][LN-2] A shared syllabus's textbook can be one whole-book PDF or one PDF per chapter (any names, any
        order, with or without a cover page). If the syllabus was set up before (some of) the PDFs were added, the
        missing chapters are added afterwards, once each, so Ask NOVA can answer. (Since 1.12.0 `nq init` loads no
        shared book: the NCERT 2018 book is retired and schools upload their own.)"""
        import json

        import pypdf

        from novaquest.core.db import Db
        from novaquest.core.migrate import bootstrap, migrate
        from novaquest.services import ingest, search, syllabus

        pdf = ROOT / "data" / "source" / "ncert_science_class8_2018.pdf"
        if not pdf.exists():
            self.skipTest("textbook PDF not present")
        map_path = ROOT / "data" / "syllabus" / "ncert_g8_science.json"
        spec = json.loads(map_path.read_text(encoding="utf-8"))
        off = spec["pdf_page_offset"]
        tmp = Path(tempfile.mkdtemp(prefix="nq-chapters-"))
        reader = pypdf.PdfReader(str(pdf))
        files: dict[int, Path] = {}
        for ch in spec["chapters"]:  # split the book like the NCERT website does: one file per chapter
            w = pypdf.PdfWriter()
            if ch["no"] % 3 == 0:
                w.add_blank_page(width=595, height=842)  # some downloads start with a cover/blank page
            for i in range(ch["start_page"] + off - 1, ch["end_page"] + off):
                w.add_page(reader.pages[i])
            files[ch["no"]] = tmp / f"hesc1{ch['no']:02d}.pdf"
            with open(files[ch["no"]], "wb") as f:
                w.write(f)
        stray = tmp / "notes.pdf"
        w = pypdf.PdfWriter()
        w.add_page(reader.pages[2])  # a front-matter page: not a chapter
        with open(stray, "wb") as f:
            w.write(f)
        whole = ingest.extract_pages(pdf)
        book = ingest.assemble_book(list(reversed(list(files.values()))) + [stray], spec["chapters"], off)
        self.assertEqual(book.chapters, tuple(range(1, 19)))
        self.assertEqual(book.unmatched, ("notes.pdf",))
        for ch in spec["chapters"]:
            self.assertEqual(ingest.build_passages(book.pages, ch, off), ingest.build_passages(whole, ch, off), ch["no"])

        name = "nq_latepdf_test"
        owner = ADMIN_URL.replace("postgres@", "nq_owner:nq_owner@").rsplit("/", 1)[0] + "/" + name
        app = owner.replace("nq_owner:nq_owner", "nq_app:nq_app")
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        try:
            bootstrap(ADMIN_URL, owner, app)
            migrate(owner, "nq_app")
            db = Db(app, 1, 2)
            db.open()
            try:
                rep = syllabus.load_syllabus(db, map_path, None, actor_id=None)
                self.assertEqual(rep.passages, 0)
                with db.anonymous() as tx:
                    self.assertEqual(syllabus.passage_count(tx, rep.syllabus_id), 0)
                    self.assertFalse(search.search(tx, rep.syllabus_id, "whats a bacteria").hits)
                added, _ = syllabus.attach_passages(db, map_path, [files[2], files[8]], actor_id=None)
                self.assertGreater(added, 20)
                with db.anonymous() as tx:
                    self.assertEqual(syllabus.chapters_with_text(tx, rep.syllabus_id), {"2", "8"})
                    self.assertTrue(search.search(tx, rep.syllabus_id, "whats a bacteria").on_syllabus)
                more, _ = syllabus.attach_passages(db, map_path, list(files.values()), actor_id=None)
                self.assertGreater(more, 350)
                self.assertEqual(syllabus.attach_passages(db, map_path, list(files.values()), actor_id=None)[0], 0, "only once")
                self.assertEqual(syllabus.attach_passages(db, map_path, [pdf], actor_id=None)[0], 0)
                with db.anonymous() as tx:
                    self.assertEqual(len(syllabus.chapters_with_text(tx, rep.syllabus_id)), 18)
                    self.assertEqual(syllabus.passage_count(tx, rep.syllabus_id), added + more)
                self.assertEqual(added + more, sum(len(ingest.build_passages(whole, ch, off)) for ch in spec["chapters"]),
                                 "the same passages as loading the whole book at once")
            finally:
                db.close()
        finally:
            with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_chapter_from_another_book_is_not_mistaken_for_ours(self):
        """[SY-1][LN-2][LN-11] A chapter PDF from another book or edition whose title contains one of ours (NCERT's
        newer 'Curiosity' book has 'Light: Mirrors and Lenses'; ours has 'Light') is skipped because its pages don't
        carry that chapter's printed page numbers. Passages an earlier version took from such a file are removed,
        and Book pages don't show it; the real chapter file is still used."""
        import json

        import pypdf
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        from novaquest.core.db import Db
        from novaquest.core.migrate import bootstrap, migrate
        from novaquest.services import ingest, pages, syllabus

        pdf = ROOT / "data" / "source" / "ncert_science_class8_2018.pdf"
        if not pdf.exists():
            self.skipTest("textbook PDF not present")
        map_path = ROOT / "data" / "syllabus" / "ncert_g8_science.json"
        spec = json.loads(map_path.read_text(encoding="utf-8"))
        off = spec["pdf_page_offset"]
        tmp = Path(tempfile.mkdtemp(prefix="nq-otherbook-"))
        reader = pypdf.PdfReader(str(pdf))

        def chapter_file(no: int) -> Path:
            ch = spec["chapters"][no - 1]
            w = pypdf.PdfWriter()
            for i in range(ch["start_page"] + off - 1, ch["end_page"] + off):
                w.add_page(reader.pages[i])
            p = tmp / f"hesc1{no:02d}.pdf"
            with open(p, "wb") as f:
                w.write(f)
            return p
        real8, real16 = chapter_file(8), chapter_file(16)
        other = tmp / "hecu110.pdf"  # another book's chapter: same word in its title, its own page numbers
        c = canvas.Canvas(str(other), pagesize=A4)
        for i in range(18):
            if i == 0:
                c.setFont("Helvetica-Bold", 22)
                c.drawString(60, 770, "Light: Mirrors and Lenses")
            c.setFont("Helvetica", 11)
            c.drawString(60, 720, "Light travels in straight lines. A plane mirror forms an image behind the mirror.")
            c.drawString(60, 700, "A convex lens converges light; a concave lens diverges it. Curiosity, Grade 8.")
            c.drawString(290, 40, str(141 + i))
            c.showPage()
        c.save()

        book = ingest.assemble_book([other, real8], spec["chapters"], off)
        self.assertEqual(book.chapters, (8,))
        self.assertEqual(book.unmatched, ("hecu110.pdf",))
        self.assertEqual(book.rejected, (("hecu110.pdf", 16),))
        light = spec["chapters"][15]
        self.assertTrue(ingest.numbered_like(ingest.extract_pages(real16), 0, light), "our own chapter 16 still matches")
        self.assertFalse(ingest.numbered_like(ingest.extract_pages(other), 0, light))
        pages._ncert_files.cache_clear() if hasattr(pages._ncert_files, "cache_clear") else None
        self.assertNotIn(16, pages._ncert_files(((str(other), 1.0),)), "Book pages never show the other book")
        self.assertIn(16, pages._ncert_files(((str(real16), 1.0),)))

        name = "nq_otherbook_test"
        owner = ADMIN_URL.replace("postgres@", "nq_owner:nq_owner@").rsplit("/", 1)[0] + "/" + name
        app = owner.replace("nq_owner:nq_owner", "nq_app:nq_app")
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        try:
            bootstrap(ADMIN_URL, owner, app)
            migrate(owner, "nq_app")
            db = Db(app, 1, 2)
            db.open()
            try:
                rep = syllabus.load_syllabus(db, map_path, None, actor_id=None)
                syllabus.attach_passages(db, map_path, [real16], actor_id=None)  # stands in for what 1.10.3 attached
                with db.anonymous() as tx:
                    self.assertEqual(syllabus.chapters_with_text(tx, rep.syllabus_id), {"16"})
                added, got = syllabus.attach_passages(db, map_path, [other, real8], actor_id=None)
                self.assertGreater(got.removed, 10, "chapter 16's passages from the wrong file are removed")
                self.assertEqual(got.removed_chapters, (16,), "only chapters that had passages are reported")
                self.assertGreater(added, 10)
                with db.anonymous() as tx:
                    self.assertEqual(syllabus.chapters_with_text(tx, rep.syllabus_id), {"8"})
                again, got = syllabus.attach_passages(db, map_path, [other, real8], actor_id=None)
                self.assertEqual((again, got.removed), (0, 0), "nothing more to do the second time")
                added16, _ = syllabus.attach_passages(db, map_path, [other, real8, real16], actor_id=None)
                self.assertGreater(added16, 10, "the right chapter 16 file still loads beside the other book's")
            finally:
                db.close()
        finally:
            with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            shutil.rmtree(tmp, ignore_errors=True)

    def test_schema_without_pgvector_then_enabled(self):
        """[NF-WIN] [NF-EMBED] Without pgvector (as on a fresh Windows install) the schema installs and search works
        on full text; `nq embed` explains how to enable it; once pgvector is added, setup adds the embedding column."""
        from novaquest.ai.embeddings import embed_missing
        from novaquest.core.db import Db
        from novaquest.core.migrate import bootstrap, migrate
        from novaquest.services import search, syllabus

        name = "nq_novector_test"
        owner = ADMIN_URL.replace("postgres@", "nq_owner:nq_owner@").rsplit("/", 1)[0] + "/" + name
        app = owner.replace("nq_owner:nq_owner", "nq_app:nq_app")
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        try:
            self.assertFalse(bootstrap(ADMIN_URL, owner, app, with_vector=False))
            migrate(owner, "nq_app")
            with psycopg.connect(owner) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone())
                self.assertIsNone(conn.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name='source_passages' AND column_name='embedding'""").fetchone())
            db = Db(app, 1, 2)  # its own pool: init_db would replace the shared test pool
            db.open()
            try:
                pdf = ROOT / "data" / "source" / "ncert_science_class8_2018.pdf"
                syllabus.load_syllabus(db, ROOT / "data" / "syllabus" / "ncert_g8_science.json", pdf if pdf.exists() else None,
                                       actor_id=None)
                search._VECTOR_READY = False
                with db.anonymous() as tx:
                    sid = tx.val("SELECT id FROM syllabi LIMIT 1")
                    res = search.search(tx, sid, "What is the function of the cell membrane?",
                                        embedder=lambda texts: [[0.0] * 768 for _ in texts])
                self.assertTrue(res.hits)
                with self.assertRaisesRegex(RuntimeError, "pgvector is not enabled"):
                    embed_missing(db, lambda texts: [[0.0] * 768 for _ in texts])
            finally:
                db.close()
            self.assertTrue(bootstrap(ADMIN_URL, owner, app))  # pgvector now available: auto-detected
            self.assertEqual(migrate(owner, "nq_app"), [])
            with psycopg.connect(owner) as conn:
                self.assertEqual(conn.execute("""SELECT udt_name FROM information_schema.columns
                    WHERE table_name='source_passages' AND column_name='embedding'""").fetchone()[0], "vector")
                self.assertTrue(conn.execute("SELECT 1 FROM pg_indexes WHERE indexname='source_passages_emb_idx'").fetchone())
        finally:
            search._VECTOR_READY = False
            with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
