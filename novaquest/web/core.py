"""Shared web plumbing: templates, the signed-in actor, CSRF, responses and error mapping."""
from __future__ import annotations

import json
import logging

import anyio.to_thread
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from novaquest import __version__, i18n
from novaquest.core import clock
from novaquest.core.config import Settings
from novaquest.core.db import Db
from novaquest.core.errors import DomainError, Forbidden, ValidationError
from novaquest.services import auth, notify
from novaquest.services.access import Actor
from novaquest.web.pictures import picture

log = logging.getLogger("novaquest.web")
HERE = Path(__file__).parent
COOKIE = "nq_session"
HOME_BY_ROLE = {"student": "/student", "teacher": "/teacher", "leader": "/leader", "inst_admin": "/admin",
                "platform_admin": "/platform"}
ROLE_NAMES = {"student": "Student", "teacher": "Teacher", "leader": "Principal / HOD", "inst_admin": "School admin",
              "platform_admin": "NovaQuest admin"}


def _ist(value: datetime | None, fmt: str = "%d %b, %I:%M %p") -> str:
    return "" if value is None else clock.to_ist(value).strftime(fmt).lstrip("0").replace(" 0", " ")


def _pct(value: Any) -> str:
    return "—" if value is None else f"{round(float(value))}%"


env = Environment(loader=FileSystemLoader(str(HERE / "templates")), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True, auto_reload=False)
env.filters["ist"] = _ist
env.filters["pct"] = _pct
env.filters["left"] = lambda v: clock.humanize_left(v) if v else ""


def _scene(text: str | None) -> str:
    """A question's own scenario, without the "Mission stage i of n." prefix (the progress bar shows that) and
    without the generic mission story, which is shown once at the start instead."""
    import re as _re

    t = _re.sub(r"^Mission stage \d+ of \d+\.\s*", "", text or "")
    return "" if t.startswith("The NovaQuest lab robot is stuck") else t


def _story(questions: list) -> str:
    """The generic mission story (shown once), if the questions use it."""
    import re as _re

    for q in questions:
        t = _re.sub(r"^Mission stage \d+ of \d+\.\s*", "", (q.get("scenario") if isinstance(q, dict) else getattr(q, "scenario", "")) or "")
        if t.startswith("The NovaQuest lab robot is stuck"):
            return t.replace(" Help it with this step.", " Help it, one step at a time.")
    return ""


def _nicenum(v) -> str:
    """1.0 -> '1', 0.5 -> '0.5' (no needless decimals in scores shown to students)."""
    try:
        f = round(float(v), 1)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(f)


env.filters["nicenum"] = _nicenum
def _part_code(item: dict) -> str:
    """'1.1', or 'P2·1.1' for a topic in part 2 of a book in parts."""
    from novaquest.services.syllabus import part_code

    return part_code(item)


env.filters["pc"] = _part_code
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sri", "smt", "shri"}
env.filters["initials"] = lambda name: "".join(w[0] for w in [x for x in str(name).replace(".", " ").split()
                                                              if x.lower() not in _TITLES][:2]).upper() or "?"  # 'Mrs Priya Rao' -> 'PR'
def _due(when):
    from novaquest.services.myprogress import due_label
    return due_label(when)


env.filters["due"] = _due  # 'Closes tomorrow, 6:00 pm' (1.31.0)
env.filters["inpart"] = lambda item: f"Part {item['part']} · " if isinstance(item, dict) and item.get("part") else ""  # 'Part 2 · '



def _share(v: Any) -> str:
    """A share of a whole as a percentage that never rounds a little progress down to nothing: 0.2 -> '<1%'."""
    v = float(v or 0)
    return "0%" if v <= 0 else ("<1%" if v < 1 else f"{round(v)}%")


env.filters["share"] = _share


def _ago(value: Any) -> str:
    """How long ago, in days, for people: 'today', 'yesterday', '9 days ago', or 'never'."""
    if value is None:
        return "never"
    days = (clock.to_ist(clock.now()).date() - clock.to_ist(value).date()).days
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


env.filters["ago"] = _ago
env.filters["scene"] = _scene
env.filters["story"] = _story
env.filters["tojson_safe"] = lambda v: json.dumps(v, default=str).replace("</", "<\\/")
env.globals.update(picture=picture, version=__version__, role_names=ROLE_NAMES, levels=("Not started", "Learning", "Practised", "Mastered"))


class Ctx:
    """Per-request context: settings, database, session."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.settings: Settings = request.app.state.settings
        self.db: Db = request.app.state.db
        self.session: auth.Session | None = request.state.session
        self.form: dict[str, Any] = {}
        self.json: Any = None

    @property
    def actor(self) -> Actor:
        if not self.session:
            raise Forbidden("Please sign in.")
        return self.session.actor

    def tenant(self):
        """Transaction scoped to the signed-in user's institution."""
        return self.db.tenant(self.actor.institution_id)

    def render(self, template: str, status: int = 200, **data: Any) -> HTMLResponse:
        """Render a page with the common layout data."""
        unread, study, lang = 0, "", "en"
        if self.session and self.session.actor.institution_id:
            with self.tenant() as tx:
                unread = notify.unread_count(tx, self.actor.user_id)
                if self.actor.role == "student":  # "Class 8B · Science, Maths" for the account menu (one query)
                    row = tx.one("""SELECT c.label, string_agg(s.subject, ', ' ORDER BY cs.ord, s.subject) AS subjects
                        FROM enrolments e JOIN classes c ON c.id=e.class_id
                        LEFT JOIN class_subjects cs ON cs.class_id=c.id
                        LEFT JOIN syllabi s ON s.id=cs.syllabus_id AND s.status='published'
                        WHERE e.student_id=%s AND e.status='active' GROUP BY c.label, e.started_at
                        ORDER BY e.started_at DESC LIMIT 1""", [self.actor.user_id])
                    if row:
                        study = f"Class {row['label']}" + (f" · {row['subjects']}" if row["subjects"] else "")
                    lang = tx.val("SELECT lang FROM users WHERE id=%s", [self.actor.user_id]) or "en"
        html = env.get_template(template).render(
            request=self.request, login_session=self.session, actor=self.session.actor if self.session else None,
            csrf=self.session.csrf if self.session else self.request.state.anon_csrf, unread=unread, study=study,
            lang=lang, tr=i18n.translator(lang), lang_name=i18n.name(lang), langs=i18n.LANGS,
            flash=self.request.query_params.get("msg"), celebrate=data.pop("celebrate", None) or _CHEERS.get(
                self.request.query_params.get("yay", "")), **data)
        return HTMLResponse(html, status_code=status)


_CHEERS = {"task": ("Handed in! 🎉", "Well done for finishing it. Your teacher will release your scorecard."),
           "escape": ("You escaped! 🎉", "You and NOVA opened all four locks. Here's what NOVA learned from you.")}


def redirect(url: str, msg: str | None = None) -> RedirectResponse:
    """303 redirect, optionally carrying a short confirmation message."""
    if msg:
        from urllib.parse import quote
        base, hash_, frag = url.partition("#")  # the message goes before any #fragment
        url = base + ("&" if "?" in base else "?") + "msg=" + quote(msg) + hash_ + frag
    return RedirectResponse(url, status_code=303)


async def form_data(request: Request, max_files: int = 4) -> dict[str, Any]:
    """Parsed form fields (multi-valued fields and names ending in [] become lists). Uploaded files are spooled to
    disk by Starlette, so large uploads don't sit in memory."""
    form = await request.form(max_files=max_files, max_fields=600, max_part_size=10 * 1024 * 1024)
    out: dict[str, Any] = {}
    for key in form.keys():
        vals = form.getlist(key)
        out[key] = vals if len(vals) > 1 or key.endswith("[]") else vals[0]
    return out


def _check_csrf(ctx: "Ctx") -> None:
    """Reject state-changing requests without the session's CSRF token."""
    expected = ctx.session.csrf if ctx.session else ctx.request.cookies.get("nq_csrf")
    sent = ctx.request.headers.get("x-csrf-token") or ctx.form.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise Forbidden("Your session expired. Reload the page and try again.", field="csrf")


Handler = Callable[["Ctx"], Response]


def page(*roles: str, api: bool = False, max_files: int = 4) -> Callable[[Handler], Callable[[Request], Awaitable[Response]]]:
    """Wrap a sync handler: require sign-in and role, parse and CSRF-check POST bodies, run the handler in a
    worker thread (services use blocking database calls), and map domain errors to friendly pages."""
    def deco(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            ctx = Ctx(request)
            try:
                if roles and not ctx.session:
                    if api:
                        return JSONResponse({"error": "Please sign in again."}, status_code=401)
                    return redirect("/login/student" if roles == ("student",) else "/login")
                if roles and ctx.actor.role not in roles:
                    raise Forbidden("You don't have access to this page.")
                if request.method == "POST":
                    ctype = request.headers.get("content-type", "")
                    if ctype.startswith("application/json"):
                        ctx.json = await request.json()
                    elif ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                        ctx.form = await form_data(request, max_files)
                    _check_csrf(ctx)
                return await anyio.to_thread.run_sync(_run, fn, ctx, api)
            except DomainError as exc:
                return await anyio.to_thread.run_sync(_error, ctx, exc, api)
        endpoint.__name__ = fn.__name__
        return endpoint
    return deco


def _run(fn: Handler, ctx: "Ctx", api: bool) -> Response:
    try:
        return fn(ctx)
    except DomainError as exc:
        return _error(ctx, exc, api)


def _error(ctx: "Ctx", exc: DomainError, api: bool) -> Response:
    if api:
        return JSONResponse({"error": exc.message, "field": exc.field}, status_code=exc.status)
    if exc.status == 403 and not ctx.session and exc.field != "csrf":
        return redirect("/login")
    return ctx.render("error.html", status=exc.status, message=exc.message)


def json_ok(**data: Any) -> JSONResponse:
    """JSON success response."""
    return JSONResponse(json.loads(json.dumps(data, default=str)))


def f_int(value: Any, name: str, *, required: bool = True) -> int | None:
    """Parse an integer form/JSON field with a friendly error."""
    if value in (None, ""):
        if required:
            raise ValidationError(f"{name} is required.", field=name)
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number.", field=name) from None


def f_list(value: Any) -> list[str]:
    """A form field that may hold one value or many."""
    if value in (None, ""):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


class Timer:
    """Measures server time per request for the Server-Timing header."""

    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000
