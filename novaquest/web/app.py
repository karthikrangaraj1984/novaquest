"""ASGI application factory. Run with: uvicorn novaquest.web.app:create_app --factory"""
from __future__ import annotations

import contextlib
import logging
import secrets
import uuid

import anyio.to_thread
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from novaquest.core.config import Settings, load_settings
from novaquest.core.db import Db, init_db
from novaquest.services import auth
from novaquest.web.core import COOKIE, HERE, Timer
from novaquest.web.routes import admin, common, learner, staff

log = logging.getLogger("novaquest.web")

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


class SessionMiddleware(BaseHTTPMiddleware):
    """Loads the session, sets security headers, request id and Server-Timing."""

    async def dispatch(self, request: Request, call_next) -> Response:
        timer = Timer()
        db: Db = request.app.state.db
        token = request.cookies.get(COOKIE)
        request.state.session = await anyio.to_thread.run_sync(auth.get_session, db, token) if token else None
        anon = request.cookies.get("nq_csrf") or secrets.token_urlsafe(16)
        request.state.anon_csrf = anon
        request.state.request_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        if not request.cookies.get("nq_csrf"):
            response.set_cookie("nq_csrf", anon, httponly=True, samesite="strict",
                                secure=request.app.state.settings.secure_cookies)
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "same-origin"
        h["X-Frame-Options"] = "DENY"
        h["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        h["X-Request-Id"] = request.state.request_id
        h["Server-Timing"] = f"app;dur={timer.ms:.1f}"
        if request.url.path.startswith("/static/"):
            h["Cache-Control"] = "public, max-age=604800, immutable" if "v=" in request.url.query else "public, max-age=3600"
        elif "Cache-Control" not in h:
            h["Cache-Control"] = "no-store"
        return response


async def healthz(request: Request) -> Response:
    """Liveness and database check."""
    db: Db = request.app.state.db

    def ping() -> bool:
        with db.anonymous() as tx:
            return tx.val("SELECT 1") == 1
    ok = await anyio.to_thread.run_sync(ping)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


async def robots(request: Request) -> Response:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def create_app(settings: Settings | None = None, db: Db | None = None) -> Starlette:
    """Build the application (settings and db can be injected by tests)."""
    settings = settings or load_settings()
    if db is None:
        db = init_db(settings.app_database_url, settings.pool_min, settings.pool_max)
    routes = [Route("/healthz", healthz), Route("/robots.txt", robots),
              Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
              *common.routes, *learner.routes, *staff.routes, *admin.routes]
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        # Handlers and AI calls run in worker threads; allow enough for a full class at once.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 120
        yield

    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(GZipMiddleware, minimum_size=800), Middleware(SessionMiddleware)])
    app.state.settings = settings
    app.state.db = db
    return app
