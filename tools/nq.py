"""NovaQuest local launcher: one command to set up and run every local service.

  python -m tools.nq init       create roles + database, apply migrations
  python -m tools.nq seed       add the small demo school (optional)
  python -m tools.nq demo       add a full demo school with several classes, teachers and 8 weeks of data
  python -m tools.nq up         start Postgres*, Mailpit, Caddy (HTTPS), the web app and the worker
  python -m tools.nq status     show what is running
  python -m tools.nq down       stop everything
  python -m tools.nq embed      add Ollama embeddings to syllabus passages (optional)
  python -m tools.nq set-key    save your Claude API key into .env (typed hidden, never shown)

* Postgres is started only when NQ_PG_BIN and NQ_PG_DATA are set (a local cluster this tool manages).
  Otherwise NovaQuest uses the Postgres at NQ_DATABASE_URL (for example the Docker Compose one)."""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAR = Path(os.environ.get("NQ_DATA_DIR", ROOT / "var"))
PIDS, LOGS = VAR / "pids", VAR / "logs"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env into the environment (existing variables win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bin(name: str) -> str | None:
    """Find a helper binary in NQ_BIN_DIR, ./bin or PATH."""
    for d in filter(None, [_env("NQ_BIN_DIR"), str(ROOT / "bin")]):
        p = Path(_exe(d, name))
        if p.exists():
            return str(p)
    return shutil.which(name)


WINDOWS = os.name == "nt"


def _add_bin_dirs_to_path() -> None:
    """Make helper programs in NQ_BIN_DIR, ./bin and NQ_EXTRA_PATH (mailpit, caddy, pdftotext) findable by
    this process and every service it starts."""
    extra = [d for d in _env("NQ_EXTRA_PATH").split(os.pathsep) if d]
    dirs = [d for d in (_env("NQ_BIN_DIR"), str(ROOT / "bin"), *extra) if d and Path(d).is_dir()]
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([d for d in dirs if d not in current] + current)


def _exe(folder: str, name: str) -> str:
    """Path of a program in a folder, with .exe on Windows."""
    return str(Path(folder) / (name + ".exe" if WINDOWS else name))


def _start(name: str, cmd: list[str], env: dict | None = None) -> None:
    PIDS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if _alive(name):
        print(f"  {name:9} already running")
        return
    log = open(LOGS / f"{name}.log", "ab")
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if WINDOWS
              else {"start_new_session": True})
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env={**os.environ, **(env or {})},
                            **detach)
    (PIDS / f"{name}.pid").write_text(str(proc.pid))
    print(f"  {name:9} started (pid {proc.pid}, log var/logs/{name}.log)")


def _pid_running(pid: int) -> bool:
    """True if a process with this id is running. On Windows os.kill(pid, 0) would terminate it, so ask the kernel."""
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_pid(pid: int) -> None:
    """Stop a started service and its child processes (uvicorn workers)."""
    if WINDOWS:
        subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(pid, signal.SIGTERM)


def _alive(name: str) -> bool:
    f = PIDS / f"{name}.pid"
    if not f.exists():
        return False
    try:
        if _pid_running(int(f.read_text())):
            return True
    except ValueError:
        pass
    f.unlink(missing_ok=True)
    return False


def _wait_http(url: str, seconds: float = 20) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with opener.open(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except OSError:
            time.sleep(0.4)
    return False


def _pg_managed() -> bool:
    return bool(_env("NQ_PG_BIN") and _env("NQ_PG_DATA"))


def pg_ctl_cmd(pg_bin: str, data: str, port: str, log: str, *args: str, windows: bool = WINDOWS) -> list[str]:
    """The pg_ctl command for the managed cluster. TCP on 127.0.0.1 only; on Linux/macOS the Unix socket lives in
    the data folder. Windows has no socket option here (and data paths there often contain spaces)."""
    opts = f"-p {port} -c listen_addresses=127.0.0.1" + ("" if windows else f" -k {data}")
    exe = str(Path(pg_bin) / ("pg_ctl.exe" if windows else "pg_ctl"))
    return [exe, "-D", data, "-l", log, "-o", opts, *args]


def initdb_cmd(pg_bin: str, data: str, *, windows: bool = WINDOWS) -> list[str]:
    """initdb for the managed cluster: superuser 'postgres', trust auth (it listens on 127.0.0.1 only), UTF-8.
    Windows has no C.UTF-8 locale, so it uses the neutral C locale with UTF-8 encoding."""
    exe = str(Path(pg_bin) / ("initdb.exe" if windows else "initdb"))
    return [exe, "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8",
            *(["--no-locale"] if windows else ["--locale=C.UTF-8"])]


def _pg_ctl(*args: str, quiet: bool = False) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)  # pg_ctl writes the server log here; on Windows a missing folder fails the start
    return subprocess.call(pg_ctl_cmd(_env("NQ_PG_BIN"), _env("NQ_PG_DATA"), _env("NQ_PG_PORT", "54329"),
                                      str(LOGS / "postgres.log"), *args), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL if quiet else None)


def _tail(path: Path, lines: int = 12) -> str:
    """The last lines of a log file, to show next to an error."""
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _pg_start() -> None:
    """Start the managed Postgres cluster, or explain clearly why it can't start and stop there (exit code 1),
    so nothing else is started against a database that isn't running."""
    data = Path(_env("NQ_PG_DATA"))
    if not (data / "PG_VERSION").exists():
        raise SystemExit(f"The database hasn't been created yet ({data}). Run  nq init  first, then  nq up.")
    if _pg_ctl("status", quiet=True) == 0:
        print("  postgres  already running")
        return
    if _pg_ctl("start", "-w") == 0:
        print("  postgres  started")
        return
    log = LOGS / "postgres.log"
    detail = _tail(log)
    raise SystemExit("  postgres  could not start.\n" + (f"Last lines of {log}:\n{detail}\n" if detail else "")
                     + "If another copy of NovaQuest is running (for example from an older folder), run  nq down  there "
                       "first. Then run  nq down  and  nq up  here.")


def cmd_init(_: argparse.Namespace) -> None:
    from novaquest.core.config import load_settings
    from novaquest.core.db import init_db
    from novaquest.core.migrate import bootstrap, migrate

    LOGS.mkdir(parents=True, exist_ok=True)
    if _pg_managed() and not Path(_env("NQ_PG_DATA"), "PG_VERSION").exists():
        print("Creating a local Postgres cluster…")
        subprocess.check_call(initdb_cmd(_env("NQ_PG_BIN"), _env("NQ_PG_DATA")), stdout=subprocess.DEVNULL)
    if _pg_managed():
        _pg_start()
    s = load_settings()
    admin = _env("NQ_ADMIN_DATABASE_URL", "postgresql://postgres@127.0.0.1:54329/postgres")
    bootstrap(admin, s.database_url, s.app_database_url)
    print("Migrations applied:", migrate(s.database_url, s.app_database_url.split("://")[1].split(":")[0]) or "none pending")
    db = init_db(s.app_database_url, 1, 2)
    try:
        with db.platform() as tx:
            books = tx.val("SELECT count(*) FROM syllabi WHERE institution_id IS NOT NULL AND status='published'")
        # The NCERT Class 8 Science book of 2018 that NovaQuest used to load here is out of date and retired (1.12.0):
        # each school uploads its own books (any board, including NCERT's current 'Curiosity') under Books.
        if not books:
            print("No school books yet. Sign in as the school admin and upload your textbooks under Books.")
        else:
            print(f"School books published: {books}.")
    finally:
        db.close()


def cmd_seed(args: argparse.Namespace) -> None:
    from tools import seed_demo
    sys.argv = ["seed_demo"] + (["--password", args.password] if args.password else [])
    seed_demo.main()


def cmd_demo(args: argparse.Namespace) -> None:
    from tools import demo_school
    sys.argv = ["demo_school"] + (["--password", args.password] if args.password else [])
    demo_school.main()


def apply_updates() -> list[str]:
    """Apply database updates that came with a new version, so upgrading is just unzip and  nq up."""
    from novaquest.core.config import load_settings
    from novaquest.core.migrate import migrate

    s = load_settings()
    return migrate(s.database_url, s.app_database_url.split("://")[1].split(":")[0])


def _keep_books() -> None:
    """Uploaded books live outside the version folder (1.14.1); copy any still in earlier version folders across."""
    from novaquest.core.config import load_settings
    from novaquest.services import books

    try:
        storage = books.storage_dir(load_settings().data_dir)
        copied = books.bring_books_across(storage, ROOT)
    except OSError as exc:
        print(f"  books     couldn't check earlier folders for uploaded books ({exc})")
        return
    if copied:
        print(f"  books     copied {len(copied)} uploaded book{'s' if len(copied) != 1 else ''} from an earlier NovaQuest "
              f"folder into {storage}")


def cmd_up(args: argparse.Namespace) -> None:
    print("Starting NovaQuest…")
    if _pg_managed():
        _pg_start()
    try:
        done = apply_updates()
        if done:
            print("  database  updated:", ", ".join(done))
    except Exception as exc:  # the database may not exist yet: the web app's own message below says to run nq init
        print(f"  database  updates not applied yet ({exc.__class__.__name__}). Run  nq init  once if this persists.")
    _keep_books()
    mailpit = _bin("mailpit")
    if mailpit:
        _start("mailpit", [mailpit, "--smtp", "127.0.0.1:1025", "--listen", "127.0.0.1:8025"])
    else:
        print("  mailpit   not found (emails stay queued until it runs)")
    workers = str(args.workers or max(1, min(4, (os.cpu_count() or 2))))
    port = _env("NQ_WEB_PORT", "8000")
    _start("web", [sys.executable, "-m", "uvicorn", "novaquest.web.app:create_app", "--factory", "--host", "127.0.0.1",
                   "--port", port, "--workers", workers, "--proxy-headers", "--no-server-header", "--timeout-keep-alive", "15"])
    _start("worker", [sys.executable, "-m", "novaquest.worker"])
    caddy = _bin("caddy")
    if caddy:
        _start("caddy", [caddy, "run", "--config", str(ROOT / "deploy" / "Caddyfile"), "--adapter", "caddyfile"],
               {"NQ_APP_UPSTREAM": f"127.0.0.1:{port}"})
    else:
        print(f"  caddy     not found: open http://127.0.0.1:{port} (no HTTPS, phones can't install the app)")
    ok = _wait_http(f"http://127.0.0.1:{port}/healthz", 40)
    if ok:
        print("Ready:", _env("NQ_BASE_URL", "https://localhost:8443"), "  ·  Mail: http://127.0.0.1:8025")
        return
    detail = _tail(LOGS / "web.log")
    print("The web app did not answer yet." + (f" Last lines of var/logs/web.log:\n{detail}" if detail else ""))
    print("If it says the database doesn't exist or a password failed, run  nq init  and then  nq up  again.")


def cmd_down(_: argparse.Namespace) -> None:
    for name in ("caddy", "worker", "web", "mailpit"):
        f = PIDS / f"{name}.pid"
        if _alive(name):
            _stop_pid(int(f.read_text()))
            print(f"  {name:9} stopped")
        f.unlink(missing_ok=True)
    if _pg_managed():
        print("  postgres  " + ("stopped" if _pg_ctl("stop", "-m", "fast", quiet=True) == 0 else "was not running"))


def cmd_status(_: argparse.Namespace) -> None:
    if _pg_managed():
        print(f"  {'postgres':9} {'running' if _pg_ctl('status', quiet=True) == 0 else 'stopped'}")
    for name in ("mailpit", "web", "worker", "caddy"):
        print(f"  {name:9} {'running' if _alive(name) else 'stopped'}")
    port = _env("NQ_WEB_PORT", "8000")
    print(f"  health    {'ok' if _wait_http(f'http://127.0.0.1:{port}/healthz', 2) else 'not responding'}")
    _book_files_status()
    from novaquest.services import ocr
    why = ocr.problem()
    print(f"  {'ocr':9} {'ready (scanned books can be read)' if not why else why + ': scanned books cannot be read (run NovaQuest-Prerequisites-Setup.exe again)'}")


def _book_files_status() -> None:
    from novaquest.core.config import load_settings
    from novaquest.core.db import init_db
    from novaquest.services import books

    s = load_settings()
    try:
        db = init_db(s.app_database_url, 1, 1)
    except Exception:  # noqa: BLE001 - the database line above already says it isn't running
        return
    try:
        missing = books.missing_files(db, books.storage_dir(s.data_dir))
    except Exception:  # noqa: BLE001
        return
    finally:
        db.close()
    if missing:
        names = sorted({m["title"] for m in missing})
        print(f"  {'books':9} {len(missing)} uploaded file(s) not on this computer ({', '.join(names)[:120]}): "
              "run  nq up  to copy them from an earlier NovaQuest folder")
    else:
        print(f"  {'books':9} uploaded books are kept in {books.storage_dir(s.data_dir)}")


def cmd_embed(_: argparse.Namespace) -> None:
    from novaquest.ai.embeddings import OllamaEmbedder, embed_missing
    from novaquest.core.config import load_settings
    from novaquest.core.db import init_db

    s = load_settings()
    if not s.embed_url:
        raise SystemExit("Set NQ_EMBED_URL (e.g. http://127.0.0.1:11434) and run `ollama pull nomic-embed-text` first.")
    db = init_db(s.app_database_url, 1, 2)
    try:
        n = embed_missing(db, OllamaEmbedder(s.embed_url, s.embed_model, s.embed_dims))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        db.close()
    print(f"Embedded {n} passages.")


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file (created from .env.example if missing), keeping every other line.
    A placeholder NQ_SECRET_KEY is replaced with a random one. The file is made readable by the owner only."""
    import secrets as _secrets

    example = ROOT / ".env.example"
    lines = path.read_text().splitlines() if path.exists() else (example.read_text().splitlines() if example.exists() else [])
    out, done = [], False
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name == key:
            out.append(f"{key}={value}")
            done = True
        elif name == "NQ_SECRET_KEY" and line.split("=", 1)[1].strip() in ("", "change-me-to-a-long-random-string"):
            out.append(f"NQ_SECRET_KEY={_secrets.token_hex(32)}")
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    restrict_to_owner(path)


def restrict_to_owner(path: Path) -> None:
    """Make a secrets file readable only by the current user (chmod 600; on Windows, an owner-only ACL)."""
    if WINDOWS:
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.call(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.chmod(path, 0o600)


def cmd_set_key(args: argparse.Namespace) -> None:
    """Save the Claude API key into .env without it appearing on screen or in shell history."""
    import getpass

    if args.from_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass("Paste your Claude API key (it starts with sk-ant-; nothing is shown as you paste): ").strip()
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise SystemExit("That doesn't look like a Claude API key (it should start with sk-ant-). Nothing was saved.")
    env_path = Path(args.env_file) if args.env_file else ROOT / ".env"
    write_env_value(env_path, "ANTHROPIC_API_KEY", key)
    print(f"Saved to {env_path} (ending …{key[-4:]}), readable only by you.")
    print("Next: python -m tools.smoke_claude   (checks the live connection), then python -m tools.nq up"
          + ("\n(With the Windows installer you can type  nq  instead of  python -m tools.nq)" if WINDOWS else ""))


def main() -> None:
    ap = argparse.ArgumentParser(prog="nq", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("seed")
    p.add_argument("--password")
    p.set_defaults(fn=cmd_seed)
    p = sub.add_parser("demo", help="add the Green Valley demo school (several classes, teachers, 8 weeks of data)")
    p.add_argument("--password", help="password for the demo staff accounts (10+ characters)")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("up")
    p.add_argument("--workers", type=int)
    p.set_defaults(fn=cmd_up)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("embed").set_defaults(fn=cmd_embed)
    p = sub.add_parser("set-key", help="save your Claude API key into .env (hidden input)")
    p.add_argument("--from-stdin", action="store_true", help="read the key from standard input (for scripts)")
    p.add_argument("--env-file", help="path of the .env file (default: ./.env)")
    p.set_defaults(fn=cmd_set_key)
    args = ap.parse_args()
    load_dotenv()
    _add_bin_dirs_to_path()
    args.fn(args)


if __name__ == "__main__":
    main()
