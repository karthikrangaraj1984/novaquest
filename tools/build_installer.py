"""Build the Windows prerequisites installer (installer/windows, Go, standard library only).

Runs the installer's unit tests, then cross-compiles a static 64-bit Windows executable.
Usage: python -m tools.build_installer [--out dist/NovaQuest-Prerequisites-Setup.exe]"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "installer" / "windows"
DEFAULT_OUT = ROOT / "dist" / "NovaQuest-Prerequisites-Setup.exe"


def build(out: Path = DEFAULT_OUT) -> Path:
    """Test and build the installer; returns the path of the .exe."""
    go = shutil.which("go") or "/usr/local/go/bin/go"
    env = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local"}
    subprocess.run([go, "vet", "./..."], cwd=SRC, env=env, check=True)
    subprocess.run([go, "test", "-count=1", "./..."], cwd=SRC, env=env, check=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-trimpath", "-ldflags", "-s -w", "-o", str(out), "."], cwd=SRC,
                   env={**env, "GOOS": "windows", "GOARCH": "amd64"}, check=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    out = build(Path(ap.parse_args().out))
    print(f"Built {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
