"""Run every test, map results to requirements, and write TEST_RESULTS.md and test_results.json.

Exit code 0 only if every test passed AND every in-scope requirement has at least one passing test.
Usage: python -m tools.run_tests [--out-dir DIR]"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"\[([A-Z]{2,5}-[A-Z0-9]+)\]")


class Recorder(unittest.TextTestResult):
    """Collects status, duration and requirement tags for every test."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.records: list[dict] = []
        self._t0 = 0.0

    def startTest(self, test) -> None:  # noqa: N802
        self._t0 = time.perf_counter()
        super().startTest(test)

    def _rec(self, test, status: str, detail: str = "") -> None:
        doc = (getattr(test, "_testMethodDoc", None) or "").strip()  # class/module setup errors have no docstring
        self.records.append({"id": test.id(), "status": status, "seconds": round(time.perf_counter() - self._t0, 2),
                             "tags": sorted(set(TAG.findall(doc))), "description": " ".join(TAG.sub("", doc).split()),
                             "detail": detail[-2000:]})

    def addSuccess(self, test) -> None:  # noqa: N802
        super().addSuccess(test)
        self._rec(test, "passed")

    def addFailure(self, test, err) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._rec(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err) -> None:  # noqa: N802
        super().addError(test, err)
        self._rec(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._rec(test, "skipped", reason)


def _env_info() -> dict:
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError):
            return "not available"
    info = {"python": platform.python_version(), "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "postgres": run(["psql", "--version"]), "cpu_count": str(__import__("os").cpu_count())}
    try:
        import psycopg
        from tests.base import ADMIN_URL
        with psycopg.connect(ADMIN_URL) as conn:
            info["postgres_server"] = conn.execute("SHOW server_version").fetchone()[0]
            info["pgvector"] = conn.execute("SELECT default_version FROM pg_available_extensions WHERE name='vector'").fetchone()[0]
    except Exception as exc:  # report, don't fail, when the environment can't be inspected
        info["postgres_server"] = f"unknown ({exc.__class__.__name__})"
    for name in ("starlette", "uvicorn", "psycopg", "jinja2", "playwright", "reportlab", "pypdf"):
        try:
            info[name] = __import__("importlib.metadata").metadata.version(name)
        except Exception:
            info[name] = "vendored source" if name == "psycopg" else "unknown"
    info["mailpit"] = run(["/opt/nqbin/mailpit", "version"])
    info["caddy"] = run(["/opt/nqbin/caddy", "version"]).split(" ")[0]
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT))
    args = ap.parse_args()
    sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]
    from tools.requirements import OUT_OF_SCOPE, REQUIREMENTS

    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    runner = unittest.TextTestRunner(resultclass=Recorder, verbosity=2, stream=sys.stdout)
    result: Recorder = runner.run(suite)
    wall = time.perf_counter() - t0
    try:
        from tests.test_performance import PERF_RESULTS
    except ImportError:
        PERF_RESULTS = {}

    by_req: dict[str, list[dict]] = {r: [] for r in REQUIREMENTS}
    unknown = set()
    for rec in result.records:
        for tag in rec["tags"]:
            if tag in by_req:
                by_req[tag].append(rec)
            elif tag not in OUT_OF_SCOPE:
                unknown.add(tag)
    req_rows = []
    for rid, text in REQUIREMENTS.items():
        recs = by_req[rid]
        if not recs:
            status = "NOT COVERED"
        elif all(r["status"] == "passed" for r in recs):
            status = "PASS"
        elif any(r["status"] in ("failed", "error") for r in recs):
            status = "FAIL"
        else:
            status = "NOT RUN"
        req_rows.append({"id": rid, "requirement": text, "status": status, "tests": [r["id"] for r in recs]})
    counts = {s: sum(1 for r in result.records if r["status"] == s) for s in ("passed", "failed", "error", "skipped")}
    release_ok = counts["failed"] == 0 and counts["error"] == 0 and counts["skipped"] == 0 and \
        all(r["status"] == "PASS" for r in req_rows) and not unknown

    out = Path(args.out_dir)
    data = {"generated_at": started.isoformat(), "duration_seconds": round(wall, 1), "release_ok": release_ok,
            "totals": {**counts, "tests": len(result.records)}, "environment": _env_info(), "performance": PERF_RESULTS,
            "requirements": req_rows, "out_of_scope": OUT_OF_SCOPE, "unknown_tags": sorted(unknown), "tests": result.records}
    (out / "test_results.json").write_text(json.dumps(data, indent=2))
    (out / "TEST_RESULTS.md").write_text(render_md(data))
    print(f"\n{'RELEASE OK' if release_ok else 'RELEASE BLOCKED'}: {counts['passed']}/{len(result.records)} tests passed, "
          f"{sum(r['status'] == 'PASS' for r in req_rows)}/{len(req_rows)} requirements pass. Results: {out / 'TEST_RESULTS.md'}")
    return 0 if release_ok else 1


def render_md(d: dict) -> str:
    """The human-readable results file."""
    t = d["totals"]
    lines = [f"# NovaQuest test results", "",
             f"**Release decision: {'APPROVED: every test and every in-scope requirement passed' if d['release_ok'] else 'BLOCKED'}**", "",
             f"- Run: {d['generated_at'][:19].replace('T', ' ')} UTC, {d['duration_seconds']} s",
             f"- Tests: {t['tests']} run · {t['passed']} passed · {t['failed']} failed · {t['error']} errors · {t['skipped']} skipped",
             f"- Requirements: {sum(r['status'] == 'PASS' for r in d['requirements'])} of {len(d['requirements'])} in scope pass", "",
             "## Requirements traceability", "", "| ID | Requirement | Result | Tests |", "|---|---|---|---|"]
    for r in d["requirements"]:
        tests = "<br>".join(x.split(".", 1)[1] for x in r["tests"]) or "—"
        lines.append(f"| {r['id']} | {r['requirement']} | {'✅ PASS' if r['status'] == 'PASS' else '❌ ' + r['status']} | {tests} |")
    lines += ["", "### Out of scope for this release", ""] + [f"- **{k}**: {v}" for k, v in d["out_of_scope"].items()]
    if d["performance"]:
        p = d["performance"]
        lines += ["", "## Performance measurements", "", "| Measure | Result | Target |", "|---|---|---|"]
        rows = [("Server render time, p95 (11 key pages × 5)", f"{p.get('render_p95_ms')} ms", "< 300 ms"),
                ("Heaviest page incl. CSS + JS, compressed", f"{p.get('heaviest_page_kb_gz')} KB", "< 60 KB"),
                ("Syllabus search, p95", f"{p.get('search_p95_ms')} ms", "< 50 ms"),
                ("Tutor hint round trip (offline engine)", f"{p.get('hint_ms_offline')} ms", "< 3 s"),
                ("Concurrent students (sign in + work)", f"{p.get('load_students')}", "100"),
                ("Autosave latency under load, p95 / median", f"{p.get('autosave_p95_ms')} / {p.get('autosave_median_ms')} ms", "< 1 s"),
                ("Errors under load", f"{p.get('load_errors')}", "0"),
                ("Server peak memory per worker under load", f"{p.get('server_peak_rss_mb_per_worker')} MB", "< 250 MB"),
                ("Memory growth, 50 parallel PIN checks", f"{p.get('hash_50_parallel_growth_mb')} MB", "< 100 MB")]
        lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    lines += ["", "## Not executed in this environment", "",
              "- **Live Claude API calls**: no API key was available in the build workspace. The Claude engine was tested against a local server "
              "that speaks the Claude Messages API (request format, model tiers, prompt caching, retries, timeouts, JSON validation, "
              "fallbacks, usage caps). Run `ANTHROPIC_API_KEY=… python -m tools.smoke_claude` on your machine to check the live connection.",
              "- **Ollama with a real embedding model**: model downloads were blocked here. The Ollama API integration and pgvector hybrid search "
              "were tested with a stand-in Ollama server. On your machine: `ollama pull nomic-embed-text`, then `python -m tools.nq embed`.",
              "- **Docker Compose**: no Docker engine in the build workspace. The same services were run and tested natively with the launcher "
              "(`tools/nq.py`); `deploy/docker-compose.yml` is provided but untested.",
              "- **Windows**: no Windows machine in the build workspace. The prerequisites installer's logic (verified downloads, "
              "safe unzip, `.env` setup, signature policy, step reporting) passed its unit tests and it builds as a 64-bit Windows "
              "program; the launcher's Windows commands and the no-pgvector schema were tested on Linux. The installer and "
              "launcher have not yet been run end to end on Windows: do one full run there before the school demo.", "",
              "## Environment", ""] + [f"- {k}: {v}" for k, v in d["environment"].items()]
    lines += ["", "## All tests", "", "| Test | Requirements | Result | Time |", "|---|---|---|---|"]
    for r in d["tests"]:
        lines.append(f"| {r['id'].split('.', 1)[1]}<br><small>{r['description']}</small> | {', '.join(r['tags']) or '—'} | "
                     f"{'✅' if r['status'] == 'passed' else '❌ ' + r['status']} | {r['seconds']} s |")
    failures = [r for r in d["tests"] if r["status"] in ("failed", "error")]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines += [f"### {r['id']}", "```", r["detail"], "```"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
