"""School admin (classes, staff, teacher mapping, rollover) and NovaQuest admin (institutions,
syllabus versions, AI cost) pages."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from starlette.responses import Response
from starlette.routing import Route

from novaquest.ai.factory import cost_report, get_engine, school_usage
from novaquest.core.errors import ValidationError
from novaquest.services import books, escape, institutions, paper, pdfs, privacy, syllabus
from novaquest.web.core import Ctx, f_int, f_list, page, redirect


@page("inst_admin")
def admin_home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        inst = tx.one("SELECT * FROM institutions WHERE id=%s", [tx.inst])
        classes = institutions.list_classes(tx)
        for c in classes:
            c["books"] = syllabus.class_subjects(tx, c["id"])
            c["book_ids"] = [i for b in c["books"] for i in b["ids"]]
        staff = institutions.list_staff(tx)
        available = syllabus.subjects(syllabus.available_books(tx))
    invite = ctx.request.query_params.get("invite")
    usage = school_usage(ctx.db, ctx.settings, ctx.actor.institution_id)
    return ctx.render("a_home.html", nav="admin", inst=inst, classes=classes, staff=staff, available=available, invite=invite,
                      next_class=institutions.next_class, usage=usage)


def _ids(values: list) -> list[int]:
    """Ticked subjects; a book in parts is one tick worth all its parts ("12,13")."""
    out: list[int] = []
    for v in values:
        for x in str(v).split(","):
            if x.strip():
                try:
                    n = int(x)
                except ValueError:
                    raise ValidationError("Choose the subjects from the list.") from None
                if n not in out:
                    out.append(n)
    return out


@page("inst_admin")
def admin_action(ctx: Ctx) -> Response:
    action, f = ctx.request.path_params["action"], ctx.form
    with ctx.tenant() as tx:
        if action == "class":
            year = tx.val("SELECT academic_year FROM institutions WHERE id=%s", [tx.inst])
            institutions.create_class(tx, ctx.actor, grade=str(f.get("grade", "")), section=str(f.get("section", "")),
                                      academic_year=str(f.get("academic_year") or year))
            msg = "Class added."
        elif action == "staff":
            inv = institutions.invite_staff(tx, ctx.actor, name=str(f.get("name", "")), email=str(f.get("email", "")),
                                            roles=f_list(f.get("roles[]") or f.get("roles")), base_url=ctx.settings.base_url)
            return redirect(f"/admin?invite={inv.link}", f"Invite sent to {inv.email}.")
        elif action == "staff_csv":
            upload = f.get("file")
            if upload is None or not getattr(upload, "filename", ""):
                raise ValidationError("Choose a CSV file.")
            invites, errors = institutions.import_staff_csv(tx, ctx.actor, upload.file.read(1_000_000), ctx.settings.base_url)
            msg = f"Invited {len(invites)} staff." + (" Problems: " + " ".join(errors) if errors else "")
        elif action == "map":
            book = f_int(f.get("syllabus_id"), "Subject (book)")
            institutions.assign_teacher(tx, ctx.actor, teacher_id=f_int(f.get("teacher_id"), "Teacher"),
                                        class_id=f_int(f.get("class_id"), "Class"), syllabus_id=book)
            msg = "Teacher mapped to class."
        elif action == "subjects":
            cid = f_int(f.get("class_id"), "Class")
            institutions.set_class_subjects(tx, ctx.actor, cid, _ids(f_list(f.get("books[]") or f.get("books"))))
            msg = "Subjects saved."
        elif action == "rollover":
            n = institutions.rollover(tx, ctx.actor, from_class_id=f_int(f.get("from_class_id"), "From class"),
                                      to_class_id=f_int(f.get("to_class_id"), "To class"))
            msg = f"Moved {n} students. Their history is kept."
        else:
            raise ValidationError("Unknown action.")
    return redirect("/admin", msg)


# ---------- school books (SY-5) ----------
def _storage(ctx: Ctx) -> Path:
    return books.storage_dir(ctx.settings.data_dir)


def _claude_ready(ctx: Ctx) -> bool:
    return getattr(get_engine(ctx.settings, ctx.db, ctx.actor.institution_id), "name", "") == "claude"


@page("inst_admin", max_files=books.MAX_FILES)
def books_home(ctx: Ctx) -> Response:
    if ctx.request.method == "POST":
        f = ctx.form
        uploads = f.get("files[]") or f.get("files") or []
        uploads = uploads if isinstance(uploads, list) else [uploads]
        try:
            sid = books.create(ctx.db, ctx.actor, board=str(f.get("board", "")), subject=str(f.get("subject", "")),
                               grade=str(f.get("grade", "")), title=str(f.get("title", "")), part=f.get("part"),
                               files=[(u.filename, u.file) for u in uploads if getattr(u, "filename", "")], storage=_storage(ctx))
        except ValidationError as exc:
            with ctx.tenant() as tx:
                items = books.list_books(tx)
            return ctx.render("a_books.html", status=422, nav="books", items=items, boards=books.BOARDS, f=f,
                              error=exc.message, claude=_claude_ready(ctx))
        return redirect(f"/admin/books/{sid}", "Uploaded. NovaQuest is reading the book; this takes a minute or two.")
    with ctx.tenant() as tx:
        items = books.list_books(tx)
        shared = [b for b in syllabus.available_books(tx) if b["institution_id"] is None]
    return ctx.render("a_books.html", nav="books", items=items, shared=shared, boards=books.BOARDS, f={},
                      claude=_claude_ready(ctx))


@page("inst_admin")
def book_page(ctx: Ctx) -> Response:
    sid = int(ctx.request.path_params["sid"])
    with ctx.tenant() as tx:
        data = books.detail(tx, sid, _storage(ctx))
    return ctx.render("a_book.html", nav="books", claude=_claude_ready(ctx), **data)


@page("inst_admin")
def book_topic(ctx: Ctx) -> Response:
    sid, tid = int(ctx.request.path_params["sid"]), int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        data = books.topic_drafts(tx, sid, tid)
        book = books.own_book(tx, sid)
    return ctx.render("a_book_topic.html", nav="books", book=book, **data)


@page("inst_admin")
def book_escape_preview(ctx: Ctx) -> Response:
    """The school admin checks a drafted escape room (with its answers) before approving it."""
    sid, ch = int(ctx.request.path_params["sid"]), int(ctx.request.path_params["ch"])
    with ctx.tenant() as tx:
        books.own_book(tx, sid)
        data = escape.guide(tx, ctx.actor, ch, sid, drafts=True)
    return ctx.render("t_escape_guide.html", nav="books", admin_view=True, **data)


@page("inst_admin")
def book_action(ctx: Ctx) -> Response:
    sid, action, f = int(ctx.request.path_params["sid"]), ctx.request.path_params["action"], ctx.form
    back = f"/admin/books/{sid}"
    if action == "delete":
        books.delete(ctx.db, ctx.actor, sid, _storage(ctx))
        return redirect("/admin/books", "Book deleted.")
    with ctx.tenant() as tx:
        if action == "publish":
            books.publish(tx, ctx.actor, sid)
            msg = "Published. Give it to classes on School setup (Subjects)."
        elif action == "archive":
            books.archive(tx, ctx.actor, sid)
            msg = "Archived. Classes no longer see this book; its history is kept."
        elif action == "part":
            books.set_part(tx, ctx.actor, sid, f.get("part"))
            msg = "Saved. Parts with the same board, subject and grade show as one subject."
        elif action == "rename":
            node = f_int(f.get("node_id"), "Topic")
            books.rename(tx, ctx.actor, sid, node, str(f.get("title", "")),
                         str(f["outcome"]) if f.get("outcome") is not None else None)
            msg, back = "Saved.", f"{back}#n{node}"
        elif action == "remove":
            books.remove_topic(tx, ctx.actor, sid, f_int(f.get("node_id"), "Topic"))
            msg = "Topic removed. Its text joined the topic next to it."
        elif action == "pages":
            on = f.get("on") == "1"
            books.set_full_pages(tx, ctx.actor, sid, on, f.get("rights") == "1")
            msg = "Students can now open this book's pages." if on else "Book pages are now hidden from students."
        elif action == "figures":
            on = f.get("on") == "1"
            books.set_teacher_figures(tx, ctx.actor, sid, on, f.get("rights") == "1")
            msg = ("Teachers now see the book's figures on their slides." if on
                   else "The book's figures are no longer shown on teachers' slides.")
        elif action == "read":
            books.read_again(tx, ctx.actor, sid)
            msg = "Reading the book again. This page updates by itself."
        elif action == "lessons":
            books.request_lessons(tx, ctx.actor, sid)
            msg = "Claude is drafting the lessons. Check back in a few minutes."
        elif action == "refresh":
            books.request_refresh(tx, ctx.actor, sid)
            msg = "Reading the book's text again. This takes a minute or two (longer for a scanned book's first read)."
        elif action == "escape":
            books.request_escape(tx, ctx.actor, sid)
            msg = "Claude is drafting the escape rooms, a few chapters at a time. Check back in a few minutes."
        elif action == "escape_approve":
            ch = f_int(f.get("chapter"), "Chapter", required=False)
            n = books.approve_escape(tx, ctx.actor, sid, ch)
            msg = f"Approved {n} escape room{'s' if n != 1 else ''}. Students and teachers can use them now."
            back = f"{back}#escape"
        elif action == "escape_discard":
            books.discard_escape(tx, ctx.actor, sid, f_int(f.get("chapter"), "Chapter"))
            msg, back = "Room removed. 'Draft escape rooms' makes a new one.", f"{back}#escape"
        elif action == "approve":
            tid = f_int(f.get("topic_id"), "Topic", required=False)
            n = books.approve(tx, ctx.actor, sid, tid)
            msg = f"Approved {n} item{'s' if n != 1 else ''}. Students can use them now."
            back = f"{back}/topics/{tid}" if tid and f.get("stay") else back
        elif action == "discard":
            tid = f_int(f.get("topic_id"), "Topic")
            books.discard(tx, ctx.actor, sid, tid, str(f.get("part", "")), str(f.get("code") or "") or None)
            msg, back = "Removed.", f"{back}/topics/{tid}"
        else:
            raise ValidationError("Unknown action.")
    return redirect(back, msg)


@page("platform_admin")
def platform_home(ctx: Ctx) -> Response:
    with ctx.db.platform() as tx:
        insts = tx.all("""SELECT i.*, (SELECT count(*) FROM enrolments e WHERE e.institution_id=i.id AND e.status='active') AS students,
                          (SELECT count(*) FROM user_roles r WHERE r.institution_id=i.id AND r.role='teacher') AS teachers
                          FROM institutions i ORDER BY i.created_at DESC""")
        versions = syllabus.list_versions(tx)
    return ctx.render("p_home.html", nav="platform", insts=insts, versions=versions, costs=cost_report(ctx.db),
                      invite=ctx.request.query_params.get("invite"))


@page("platform_admin")
def platform_action(ctx: Ctx) -> Response:
    action, f = ctx.request.path_params["action"], ctx.form
    if action == "institution":
        _, inv = institutions.create_institution(
            ctx.db, ctx.actor, name=str(f.get("name", "")), kind=str(f.get("kind", "school")), board=str(f.get("board", "CBSE")),
            academic_year=str(f.get("academic_year", "")), school_code=str(f.get("school_code", "")),
            admin_name=str(f.get("admin_name", "")), admin_email=str(f.get("admin_email", "")), base_url=ctx.settings.base_url)
        return redirect(f"/platform?invite={inv.link}", "Institution created. Its admin has been invited.")
    if action == "syllabus":
        spec, pdf = f.get("map"), f.get("pdf")
        if spec is None or not getattr(spec, "filename", ""):
            raise ValidationError("Choose the syllabus map (JSON).")
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "map.json"
            raw = spec.file.read(5_000_000)
            try:
                json.loads(raw)
            except ValueError:
                raise ValidationError("The syllabus map is not valid JSON.") from None
            mp.write_bytes(raw)
            pp = None
            if pdf is not None and getattr(pdf, "filename", ""):
                pp = Path(d) / "book.pdf"
                with pp.open("wb") as out:
                    while chunk := pdf.file.read(1 << 20):
                        out.write(chunk)
            rep = syllabus.load_syllabus(ctx.db, mp, pp, actor_id=ctx.actor.user_id)
        return redirect("/platform", f"Loaded version with {rep.topics} topics and {rep.passages} passages.")
    raise ValidationError("Unknown action.")


# ---------- data protection (1.19.0) ----------
_PRIVACY_ACTIONS = {"privacy.export": "Data exported", "privacy.erase": "Student erased", "privacy.retention": "Retention changed",
                    "privacy.consent_withdrawn": "Consent withdrawn", "privacy.retention_run": "Daily clean-up",
                    "privacy.ai_log_view": "AI conversations viewed"}


@page("inst_admin")
def privacy_home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        d = privacy.overview(tx, ctx.actor)
    if ctx.request.query_params.get("format") == "csv":
        return Response(privacy.register_csv(d["register"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=consent-register.csv"})
    return ctx.render("a_privacy.html", nav="privacy", d=d, choices=privacy.MONTH_CHOICES, methods=privacy.METHODS,
                      actions=_PRIVACY_ACTIONS)


@page("inst_admin")
def privacy_notice(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        school = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
        pdf = pdfs.privacy_notice(school, privacy.settings(tx))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=novaquest-parent-notice.pdf"})


@page("inst_admin")
def privacy_export(ctx: Ctx) -> Response:
    sid = int(ctx.request.path_params["sid"])
    with ctx.tenant() as tx:
        data = privacy.export_student(tx, ctx.actor, sid)
    body = json.dumps(data, default=str, indent=1, ensure_ascii=False)
    return Response(body, media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=novaquest-student-{sid}.json"})


@page("inst_admin")
def privacy_action(ctx: Ctx) -> Response:
    action, f = ctx.request.path_params["action"], ctx.form
    with ctx.tenant() as tx:
        if action == "retention":
            privacy.save_settings(tx, ctx.actor, f_int(f.get("ai_chat_months"), "Conversations with NOVA"),
                                  f_int(f.get("left_student_months"), "Students who have left"))
            msg = "Saved."
        elif action == "withdraw":
            privacy.withdraw_consent(tx, ctx.actor, f_int(f.get("enrolment_id"), "Student"))
            msg = "Consent withdrawn. The student is signed out until consent is recorded again."
        else:
            raise ValidationError("Unknown action.")
    return redirect("/admin/privacy", msg)


@page("inst_admin")
def privacy_erase(ctx: Ctx) -> Response:
    sid = int(ctx.request.path_params["sid"])
    with ctx.tenant() as tx:
        privacy.erase_student(tx, ctx.actor, sid, str(ctx.form.get("confirm", "")),
                              paper_root=paper.storage_dir(ctx.settings.data_dir))
    return redirect("/admin/privacy", "The student's personal data has been erased.")


@page("leader", "inst_admin")
def ai_log(ctx: Ctx) -> Response:
    qp = ctx.request.query_params
    days = f_int(qp.get("days"), "Period", required=False) or 30
    with ctx.tenant() as tx:
        log = privacy.ai_log(tx, ctx.actor, class_id=f_int(qp.get("class"), "Class", required=False), q=str(qp.get("q", ""))[:80],
                             days=days if days in (7, 30, 90, 365) else 30)
    return ctx.render("l_ailog.html", nav="ailog", log=log)


routes = [Route("/admin", admin_home),
          Route("/admin/privacy", privacy_home), Route("/admin/privacy/notice", privacy_notice),
          Route("/admin/privacy/students/{sid:int}/export", privacy_export),
          Route("/admin/privacy/students/{sid:int}/erase", privacy_erase, methods=["POST"]),
          Route("/admin/privacy/{action}", privacy_action, methods=["POST"]), Route("/leader/ai-log", ai_log),
          Route("/admin/books", books_home, methods=["GET", "POST"]),
          Route("/admin/books/{sid:int}", book_page),
          Route("/admin/books/{sid:int}/topics/{tid:int}", book_topic),
          Route("/admin/books/{sid:int}/escape/{ch:int}", book_escape_preview),
          Route("/admin/books/{sid:int}/{action}", book_action, methods=["POST"]),
          Route("/admin/{action}", admin_action, methods=["POST"]),
          Route("/platform", platform_home), Route("/platform/{action}", platform_action, methods=["POST"])]
