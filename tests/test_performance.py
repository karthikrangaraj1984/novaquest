"""Performance, scale and memory (NF-PERF, NF-SCALE, NF-MEM)."""
from __future__ import annotations

import gzip
import os
import re
import resource
import signal
import socket
import subprocess
import sys
import threading
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from novaquest.core import clock
from novaquest.services import search, students
from tests.base import PASSWORD, ROOT, NQTestCase
from tests.live import Client, LiveServer

PERF_RESULTS: dict[str, float] = {}


class ServerProcess:
    """Uvicorn as its own process with several workers, against the test database."""

    def __init__(self, workers: int = 2) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / ".vendor"), str(ROOT)]), "NQ_POOL_MAX": "20"}
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "novaquest.web.app:create_app", "--factory", "--host", "127.0.0.1",
                                      "--port", str(self.port), "--workers", str(workers), "--log-level", "warning"],
                                     env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.url = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            try:
                if Client(self.url).get("/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)

    def _pids(self) -> list[int]:
        out = subprocess.run(["pgrep", "-g", str(os.getpgid(self.proc.pid))], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    def peak_rss_mb(self) -> float:
        """Highest peak RSS (VmHWM) of any one server process."""
        peaks = []
        for pid in self._pids():
            try:
                for line in open(f"/proc/{pid}/status"):
                    if line.startswith("VmHWM:"):
                        peaks.append(int(line.split()[1]) / 1024)
            except OSError:
                pass
        return round(max(peaks or [0.0]), 1)

    def stop(self) -> None:
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        self.proc.wait(10)


def p95(xs: list[float]) -> float:
    return sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]


class Performance(NQTestCase):
    def setUp(self):
        super().setUp()
        self.clock.set(clock.Clock().now())  # real time, so the live server and the data agree
        self.live = LiveServer(self.settings, self.db)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def test_server_render_budget_and_page_weight(self):
        """[NF-PERF][LN-10] Pages, including the largest picture lessons, render on the server in under 300 ms (p95)
        and weigh under 60 KB compressed, so they load
        in under 2 s on a mid-range phone over 4G."""
        school = self.make_school(n_students=30)
        aid = self.open_assignment(school)
        t = Client(self.live.url)
        t.login("rao@sun.test", PASSWORD)
        s = Client(self.live.url)
        s.login_student("SUN", "8B", "1", school.students[0]["pin"])
        timings, weights = [], {}
        pages = [(t, "/teacher"), (t, f"/teacher/assignments/{aid}/review"), (t, "/teacher/syllabus"),
                 (t, f"/teacher/classes/{school.class_id}/mastery"), (s, "/student"), (s, "/student/learn"),
                 (s, f"/student/learn/{school.topics['8.5']}"), (s, "/student/progress"),
                 (s, f"/student/learn/{school.topics['8.5']}/pictures"), (s, f"/student/learn/{school.topics['9.2']}/pictures"),
                 (s, "/student/pictures")]
        for _ in range(5):
            for c, p in pages:
                status, html, h, _ = c.get(p)
                self.assertEqual(status, 200)
                timings.append(float(re.search(r"app;dur=([\d.]+)", h["server-timing"]).group(1)))
                weights[p] = len(gzip.compress(html.encode()))
        css = len(gzip.compress((self.live_static("app.css")).encode()))
        js = len(gzip.compress((self.live_static("app.js")).encode()))
        heaviest = max(weights.values()) + css + js
        PERF_RESULTS.update({"render_p95_ms": round(p95(timings), 1), "render_median_ms": round(statistics.median(timings), 1),
                             "heaviest_page_kb_gz": round(heaviest / 1024, 1)})
        self.assertLess(p95(timings), 300, timings)
        self.assertLess(heaviest, 60 * 1024)

    def live_static(self, name: str) -> str:
        return Client(self.live.url).get(f"/static/{name}")[1]

    def test_search_and_hint_latency(self):
        """[NF-PERF] Syllabus search answers in under 50 ms (p95) and the tutor hint path stays well under 3 s."""
        school = self.make_school()
        qs = ["What does the nucleus do?", "Why do we see the moon change shape?", "How does a fire extinguisher work?",
              "What is friction?", "Can sound travel in space?", "What is a red data book?"]
        times = []
        with self.db.tenant(school.inst) as tx:
            for _ in range(5):
                for q in qs:
                    t = time.perf_counter()
                    search.search(tx, school.syllabus_id, q)
                    times.append((time.perf_counter() - t) * 1000)
        aid = self.open_assignment(school, kind="quiz", count=2)
        s = Client(self.live.url)
        s.login_student("SUN", "8B", "1", school.students[0]["pin"])
        _, html, _, url = s.get(f"/student/assignments/{aid}")
        att = re.search(r"/student/attempts/(\d+)", url).group(1)
        qid = re.search(r'data-q="(\d+)"', html).group(1)
        t = time.perf_counter()
        code, _, _ = s.post_json(f"/api/attempts/{att}/questions/{qid}/hint", {})
        hint_ms = (time.perf_counter() - t) * 1000
        PERF_RESULTS.update({"search_p95_ms": round(p95(times), 1), "hint_ms_offline": round(hint_ms, 1)})
        self.assertEqual(code, 200)
        self.assertLess(p95(times), 50)
        self.assertLess(hint_ms, 3000)

    def test_hundred_students_at_once(self):
        """[NF-SCALE] 100 students sign in and work on an assignment at the same time (server with 2 workers, as in production)
        with no errors, autosave p95 under 1 s, and bounded server memory. [NF-MEM]"""
        school = self.make_school(n_students=1)
        with self.db.tenant(school.inst) as tx:
            rows = [{"roll_no": str(i), "name": f"Load {i}", "parent_email": ""} for i in range(2, 102)]
            added = students.import_rows(tx, school.teacher, school.class_id, rows, skip_invalid=False).added
            tx.exec("UPDATE enrolments SET consent_status='recorded'")
        aid = self.open_assignment(school, kind="quiz", count=5)
        server = ServerProcess(workers=2)
        errors: list[str] = []
        saves: list[float] = []
        clients: list[tuple[Client, str, list[str]]] = []
        lock = threading.Lock()

        def arrive(a: dict) -> None:
            c = Client(server.url)
            status, _, _, url = c.login_student("SUN", "8B", a["roll_no"], a["pin"])
            if url != "/student":
                errors.append(f"login {a['roll_no']} {status}")
                return
            status, html, _, url = c.get(f"/student/assignments/{aid}")
            m = re.search(r"/student/attempts/(\d+)", url)
            if not m:
                errors.append(f"open {status}")
                return
            with lock:
                clients.append((c, m.group(1), re.findall(r'data-q="(\d+)"', html)))

        def work(item: tuple) -> None:
            c, att, qids = item
            for qid in qids:
                for choice in (0, 1):
                    t = time.perf_counter()
                    code, _, _ = c.post_json(f"/api/attempts/{att}/answers", {"question_id": int(qid), "answer": {"index": choice}})
                    with lock:
                        saves.append((time.perf_counter() - t) * 1000)
                    if code != 200:
                        errors.append(f"save {code}")
        try:
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=100) as pool:
                list(pool.map(arrive, added))
            login_wall = time.perf_counter() - t0
            t1 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=100) as pool:
                list(pool.map(work, clients))
            work_wall = time.perf_counter() - t1
            server_mb = server.peak_rss_mb()
        finally:
            server.stop()
        with self.db.tenant(school.inst) as tx:
            n_att = tx.val("SELECT count(*) FROM attempts WHERE assignment_id=%s", [aid])
            n_resp = tx.val("SELECT count(*) FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE t.assignment_id=%s", [aid])
        PERF_RESULTS.update({"load_students": 100, "load_logins_wall_s": round(login_wall, 1), "load_work_wall_s": round(work_wall, 1),
                             "autosave_requests": len(saves), "autosave_p95_ms": round(p95(saves), 1),
                             "autosave_median_ms": round(statistics.median(saves), 1), "load_errors": len(errors),
                             "server_peak_rss_mb_per_worker": server_mb})
        self.assertEqual(errors, [])
        self.assertEqual((n_att, n_resp), (100, 500))
        self.assertLess(p95(saves), 1000)
        self.assertLess(server_mb, 250)

    def test_memory_bounded(self):
        """[NF-MEM] Memory stays bounded: caches are size-limited and password hashing is capped at 4 at a time."""
        from novaquest.core import security
        from novaquest.services import search as s, syllabus as sy
        self.assertLessEqual(len(s._IDF), 8)
        self.assertLessEqual(len(sy.get_tree._data), 8)
        self.assertEqual(security._HASH_SLOTS._initial_value, 4)
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        with ThreadPoolExecutor(max_workers=50) as pool:
            list(pool.map(lambda _: security.hash_secret("1234"), range(50)))
        grew = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 - before
        PERF_RESULTS["hash_50_parallel_growth_mb"] = round(grew, 1)
        self.assertLess(grew, 100, "50 parallel logins must not need 50 x 16 MB")
