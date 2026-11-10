"""Sign-in, invites, notifications, role switching, parent report links and the service worker."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from novaquest.core.errors import DomainError
from novaquest.services import auth, notify, reports
from novaquest.web.core import COOKIE, HERE, HOME_BY_ROLE, Ctx, page, redirect

ALL = ("student", "teacher", "leader", "inst_admin", "platform_admin")


def _set_cookie(ctx: Ctx, resp: Response, token: str) -> Response:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=ctx.settings.secure_cookies, max_age=12 * 3600)
    return resp


@page()
def home(ctx: Ctx) -> Response:
    if ctx.session:
        return redirect(HOME_BY_ROLE[ctx.actor.role])
    return redirect("/login/student")


@page()
def login(ctx: Ctx) -> Response:
    if ctx.request.method == "GET":
        return ctx.render("login.html", email="")
    email = str(ctx.form.get("email", ""))
    try:
        sess = auth.login_email(ctx.db, ctx.settings, email, str(ctx.form.get("password", "")))
    except DomainError as exc:
        return ctx.render("login.html", status=exc.status, error=exc.message, email=email)
    return _set_cookie(ctx, redirect(HOME_BY_ROLE[sess.actor.role]), sess.token)


@page()
def login_student(ctx: Ctx) -> Response:
    if ctx.request.method == "GET":
        return ctx.render("login_student.html", f={})
    f = {k: str(ctx.form.get(k, "")) for k in ("school", "klass", "roll")}
    try:
        sess = auth.login_pin(ctx.db, ctx.settings, f["school"], f["klass"], f["roll"], str(ctx.form.get("pin", "")))
    except DomainError as exc:
        return ctx.render("login_student.html", status=exc.status, error=exc.message, f=f)
    return _set_cookie(ctx, redirect("/student"), sess.token)


@page(*ALL)
def logout(ctx: Ctx) -> Response:
    auth.logout(ctx.db, ctx.session.token)
    resp = redirect("/login/student" if ctx.actor.role == "student" else "/login")
    resp.delete_cookie(COOKIE)
    return resp


@page()
def invite(ctx: Ctx) -> Response:
    token = ctx.request.path_params["token"]
    if ctx.request.method == "GET":
        return ctx.render("invite.html", token=token)
    pw, pw2 = str(ctx.form.get("password", "")), str(ctx.form.get("password2", ""))
    if pw != pw2:
        return ctx.render("invite.html", status=422, token=token, error="The two passwords don't match.")
    try:
        auth.accept_invite(ctx.db, token, pw)
    except DomainError as exc:
        return ctx.render("invite.html", status=exc.status, token=token, error=exc.message)
    return redirect("/login", "Password set. Sign in with your email.")


@page(*ALL)
def switch_role(ctx: Ctx) -> Response:
    sess = auth.switch_role(ctx.db, ctx.session, str(ctx.form.get("role", "")))
    return redirect(HOME_BY_ROLE[sess.actor.role])


@page("student", "teacher", "leader", "inst_admin")
def notifications(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            notify.mark_read(tx, ctx.actor.user_id)
            return redirect("/notifications")
        items = notify.unread(tx, ctx.actor.user_id, 50)
    layout = "student.html" if ctx.actor.role == "student" else "staff.html"
    return ctx.render("notifications.html", items=items, layout=layout, nav="notifications")


@page()
def shared_report(ctx: Ctx) -> Response:
    """A parent's link opens a simple page on the phone (from WhatsApp, say), with the PDF one tap away."""
    token = ctx.request.path_params["token"]
    view = reports.shared_view(ctx.db, token)
    resp = ctx.render("r_report.html", token=token, **view)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@page()
def shared_report_pdf(ctx: Ctx) -> Response:
    pdf = reports.shared_pdf(ctx.db, ctx.request.path_params["token"])
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=progress-report.pdf",
                                                              "Cache-Control": "no-store"})


@page()
def offline(ctx: Ctx) -> Response:
    return ctx.render("offline.html")


async def service_worker(request: Request) -> Response:
    return FileResponse(HERE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


async def manifest(request: Request) -> Response:
    return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")


routes = [Route("/", home), Route("/login", login, methods=["GET", "POST"]),
          Route("/login/student", login_student, methods=["GET", "POST"]), Route("/logout", logout, methods=["POST"]),
          Route("/invite/{token}", invite, methods=["GET", "POST"]), Route("/role", switch_role, methods=["POST"]),
          Route("/notifications", notifications, methods=["GET", "POST"]), Route("/r/{token}.pdf", shared_report_pdf), Route("/r/{token}", shared_report),
          Route("/offline", offline), Route("/sw.js", service_worker), Route("/manifest.webmanifest", manifest)]
