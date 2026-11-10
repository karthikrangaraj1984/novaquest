"""Release gate: run every test; build the release package only if all tests and all in-scope
requirements pass. Usage: python -m tools.release"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["novaquest", "tools", "tests", "deploy", "data/syllabus", "data/pictures", "data/escape", "README.md", "CODING_STANDARDS.md", "CHANGELOG.md",
           "TEST_RESULTS.md", "test_results.json", "requirements.txt", "requirements-dev.txt", ".env.example", ".gitignore", "installer"]
SKIP_PARTS = {"__pycache__", ".vendor", "var", ".pytest_cache"}


def main() -> int:
    env = {**__import__("os").environ, "PYTHONPATH": f"{ROOT / '.vendor'}:{ROOT}"}
    code = subprocess.call([sys.executable, "-m", "tools.run_tests"], cwd=ROOT, env=env)
    results = json.loads((ROOT / "test_results.json").read_text())
    if code != 0 or not results["release_ok"]:
        print("Release blocked: see TEST_RESULTS.md. Nothing was packaged.")
        return 1
    version = __import__("novaquest").__version__
    dist = ROOT / "dist"
    from tools.build_installer import build
    setup_exe = build(dist / "NovaQuest-Prerequisites-Setup.exe")
    dist.mkdir(exist_ok=True)
    out = dist / f"novaquest-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in INCLUDE:
            p = ROOT / item
            files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
            for f in files:
                rel = f.relative_to(ROOT)
                if SKIP_PARTS & set(rel.parts) or f.suffix in (".pyc",):
                    continue
                z.write(f, f"novaquest-{version}/{rel}")
        z.write(setup_exe, f"novaquest-{version}/{setup_exe.name}")  # run from the unzipped folder on Windows
    with zipfile.ZipFile(out) as z:  # the package must carry every lesson and drawing the app validates on start
        names = set(z.namelist())
        need = [f"novaquest-{version}/data/{d}/{f.name}" for d in ("pictures", "escape", "syllabus")
                for f in sorted((ROOT / "data" / d).glob("*.json"))]
        need += [f"novaquest-{version}/data/{d.name}/" for d in (ROOT / "data").iterdir()
                 if d.is_dir() and d.name != "source" and not any(n.startswith(f"novaquest-{version}/data/{d.name}/") for n in names)]
        need += [f"novaquest-{version}/novaquest/web/scenes/{f.name}" for f in sorted((ROOT / "novaquest" / "web" / "scenes").glob("*.svg"))]
        missing = [n for n in need if n not in names]
        if missing:
            out.unlink()
            print(f"Release blocked: package is missing {len(missing)} content files, e.g. {missing[0]}")
            return 1
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    exe_digest = hashlib.sha256(setup_exe.read_bytes()).hexdigest()
    (dist / f"novaquest-{version}.zip.sha256").write_text(f"{digest}  {out.name}\n{exe_digest}  {setup_exe.name}\n")
    t = results["totals"]
    print(f"Released {out.name}: {t['passed']}/{t['tests']} tests passed, "
          f"{sum(r['status'] == 'PASS' for r in results['requirements'])}/{len(results['requirements'])} requirements. sha256 {digest[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
