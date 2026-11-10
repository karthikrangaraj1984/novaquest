"""Teacher and leadership pages: home, assignment builder and review, results review, syllabus board,
students, mastery, question bank, tutor logs, Teach NOVA transcripts, and dashboards with exports."""
from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from novaquest.ai.factory import get_engine
from novaquest.core import clock
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import (assignments, books, briefing, dashboards, paper, escape, exams, growth, learn, mastery, pages, pdfs, pictures, plans,
                                prep, principal, questions, reports, review, students, syllabus, teach, timetable, today, worksheets)
from novaquest.services import doubts, myprogress
from novaquest.services import figures
from novaquest.services import present as decks
from novaquest.services.access import require_class, visible_class_ids
from novaquest.web.core import Ctx, f_int, f_list, page, redirect

T = "teacher"
STAFF = ("teacher", "leader", "inst_admin")


def _engine(ctx: Ctx):
    return get_engine(ctx.settings, ctx.db, ctx.actor.institution_id)


def _classes(ctx: Ctx, tx) -> list[dict]:
    ids = visible_class_ids(tx, ctx.actor)
    return tx.all("SELECT id, label FROM classes WHERE id = ANY(%s) ORDER BY label", [ids])


def _teaching(ctx: Ctx, tx) -> list[dict]:
    """Every (class, book) this staff member works with: what a teacher picks before choosing topics (SY-6)."""
    return today.teaching(tx, ctx.actor)


def _pick_teaching(teaching: list[dict], key: str | None = None, class_id: int | None = None,
                   book_id: int | None = None) -> dict | None:
    """The chosen (class, book): by 'cid:sid' key (any part of a book in parts), else by class and/or book, else the
    first."""
    if key and ":" in key:
        cid, _, sid = key.partition(":")
        if cid.isdigit() and sid.isdigit():
            class_id, book_id = int(cid), int(sid)
    for t in teaching:
        if (class_id is None or t["cid"] == class_id) and (book_id is None or book_id in t["book"]["ids"]) and (class_id or book_id):
            return t
    return teaching[0] if teaching else None


def _slips_signer(ctx: Ctx) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.settings.secret_key, salt="login-slips")


# ---------- home and assignments ----------
@page(T)
def home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = dashboards.teacher_home(tx, ctx.actor)
    return ctx.render("t_home.html", nav="home", **data)


@page(T)
def new_assignment(ctx: Ctx) -> Response:
    f = ctx.form if ctx.request.method == "POST" else {}
    q = ctx.request.query_params
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
        teaching = _teaching(ctx, tx)
        pre = q.get("topic")
        pre_book = syllabus.topic_syllabus_id(tx, int(pre)) if pre and pre.isdigit() else None
        topic_ids = [int(t) for t in f_list(f.get("topics[]") or f.get("topics"))] if f else []
        if topic_ids and not f.get("for"):
            pre_book = syllabus.topic_syllabus_id(tx, topic_ids[0])
        cur = _pick_teaching(teaching, str(f.get("for") or q.get("for") or ""),
                             f_int(f.get("class_id") or q.get("class"), "Class", required=False), pre_book)
        tree = syllabus.subject_tree(tx, cur["book"]) if cur else ()
        in_class = {p["chapter_id"] for p in plans.in_class_now(tx, cur["cid"]) if p["syllabus_id"] in cur["book"]["ids"]} \
            if cur and ctx.request.method == "GET" else set()
    for_students = str(f.get("for_students") or q.get("students") or "")
    for_students = ",".join(x for x in for_students.split(",") if x.isdigit())
    others = [t for t in today.sections(teaching, cur) if t["key"] != cur["key"]] if cur else []
    ticked = {int(x) for x in f_list(f.get("sections[]")) if str(x).isdigit()} if f else {o["cid"] for o in others}
    view = {"nav": "assignments", "classes": classes, "teaching": teaching, "cur": cur, "tree": tree, "kinds": questions.KINDS,
            "for_students": for_students, "others": others, "ticked": ticked}
    if ctx.request.method == "GET":
        chosen = {int(x) for x in (q.get("topics") or "").split(",") if x.isdigit()} | ({int(pre)} if pre and pre.isdigit() else set())
        chosen |= {t["id"] for ch in tree if str(ch["id"]) == q.get("chapter") for t in ch["topics"]}  # from the term plan
        from_plan = False
        if not chosen and not for_students and in_class:  # start from the chapter taught now (term plan); untick the rest
            chosen, from_plan = {t["id"] for ch in tree if ch["id"] in in_class for t in ch["topics"]}, True
        view.update(in_class=in_class, from_plan=from_plan)
        return ctx.render("t_new.html", **view, chosen=chosen, f={"kind": q.get("kind")} if q.get("kind") else {})
    try:
        if not cur:
            raise ValidationError("You aren't mapped to a class and subject yet. Ask your school admin.")
        wanted = f_int(f.get("count") or 5, "Number of questions")
        engine = _engine(ctx)
        aid = questions.generate_assignment(ctx.db, engine, ctx.actor, class_id=cur["cid"],
                                            topic_ids=topic_ids, kind=str(f.get("kind", "")), difficulty=str(f.get("difficulty", "")),
                                            count=wanted, title=str(f.get("title", "")))
    except ValidationError as exc:
        return ctx.render("t_new.html", status=422, **view, chosen=set(topic_ids), f=f, error=exc.message)
    with ctx.tenant() as tx:
        if ticked and not for_students:
            assignments.set_for_sections(tx, ctx.actor, aid, sorted(ticked))
        made = tx.val("SELECT count(*) FROM assignment_questions WHERE assignment_id=%s", [aid])
        basic = tx.val("""SELECT count(*) FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                          WHERE aq.assignment_id=%s AND q.written_by='offline'""", [aid])
    kind = str(f.get("kind", ""))
    dest = f"/teacher/assignments/{aid}" + (f"?students={for_students}" if for_students else "")
    if kind == "escape":
        return redirect(dest, "Escape room ready. Choose who gets it and when: it's handed in when each student escapes.")
    if basic:
        why = getattr(engine, "last_error", "")
        return redirect(dest, f"Draft ready, but Claude couldn't write {basic} of the {made} questions"
                        + (f" ({why})" if why else "") + ". Those are marked Basic: rewrite them with Claude, or check them carefully.")
    if kind not in ("learn", "teach") and made < wanted:
        return redirect(dest, f"Draft ready with {made} of the {wanted} questions you asked for: "
                        "that's all NovaQuest could write well from these topics. Add topics, or add your own questions.")
    return redirect(dest, "Draft ready. Check each question, then schedule it." +
                    (" It's set for the students you chose." if for_students else ""))


@page(T)
def assignment_list(ctx: Ctx) -> Response:
    q = ctx.request.query_params
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
        items = assignments.for_teacher(tx, ctx.actor, [c["id"] for c in classes], limit=300)
    cls = f_int(q.get("class"), "class", required=False)
    show = q.get("show") if q.get("show") in ("action", "open", "draft", "done") else "all"
    stage = {"action": lambda a: a["flagged"] or (a["live_state"] == "closed" and not a["released_at"]),
             "open": lambda a: a["live_state"] in ("open", "scheduled"), "draft": lambda a: a["live_state"] == "draft",
             "done": lambda a: a["live_state"] == "reviewed", "all": lambda a: True}
    counts = {k: sum(1 for a in items if f(a) and (cls is None or a["class_id"] == cls)) for k, f in stage.items()}
    shown = [a for a in items if stage[show](a) and (cls is None or a["class_id"] == cls)]
    return ctx.render("t_assignments.html", nav="assignments", items=shown, classes=classes, cls=cls, show=show, counts=counts)


@page(*STAFF)
def assignment_detail(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        a = assignments.get(tx, aid)
        require_class(tx, ctx.actor, a["class_id"])
        if a["live_state"] in ("open", "closed", "reviewed") and ctx.request.query_params.get("view") != "questions":
            return redirect(f"/teacher/assignments/{aid}/review")  # once it's open, the teacher wants progress first
        qs = questions.assignment_questions(tx, aid)
        students_ = students.list_students(tx, a["class_id"])
        groups = assignments.list_groups(tx, a["class_id"])
        mis = {r["code"]: r["label"] for r in tx.all("SELECT code, label FROM misconceptions")}
        now_ist = clock.to_ist(clock.now())
        fallback_close = now_ist.replace(hour=18, minute=0) + __import__("datetime").timedelta(days=3)
        section_rows = []
        if a["state"] == "draft" and ctx.actor.role == "teacher":
            for c in [{"id": a["class_id"], "label": a["class_label"], "ticked": True, "own": True}] + assignments.section_choices(tx, ctx.actor, a):
                due = timetable.due_by(tx, c["id"], prep.subject_ids(tx, c["id"], a["syllabus_id"]))
                section_rows.append({**c, "own": c.get("own", False), "opens": now_ist.strftime("%Y-%m-%dT%H:%M"),
                                     "closes": clock.to_ist(due).strftime("%Y-%m-%dT%H:%M") if due else fallback_close.strftime("%Y-%m-%dT%H:%M"),
                                     "from_timetable": bool(due)})
        sibs = assignments.siblings(tx, a)
    levels: dict[str, int] = {}
    for q in qs:
        levels[q["thinking_level"]] = levels.get(q["thinking_level"], 0) + 1
    preselect = {int(x) for x in (ctx.request.query_params.get("students") or "").split(",") if x.isdigit()}
    return ctx.render("t_draft.html", nav="assignments", a=a, qs=qs, students=students_, groups=groups, mis=mis, levels=levels,
                      preselect=preselect, section_rows=section_rows, siblings=sibs,
                      approved=sum(q["status"] == "approved" for q in qs), kinds=questions.KINDS,
                      default_open=section_rows[0]["opens"] if section_rows else now_ist.strftime("%Y-%m-%dT%H:%M"),
                      default_close=section_rows[0]["closes"] if section_rows else fallback_close.strftime("%Y-%m-%dT%H:%M"))


@page(T)
def question_action(ctx: Ctx) -> Response:
    qid, action = int(ctx.request.path_params["qid"]), ctx.request.path_params["action"]
    aid = f_int(ctx.form.get("assignment_id"), "assignment")
    if action == "regenerate":
        questions.regenerate(ctx.db, _engine(ctx), ctx.actor, qid)
        return redirect(f"/teacher/assignments/{aid}#q-list", "New question written. Check it below.")
    with ctx.tenant() as tx:
        if action == "approve":
            questions.approve(tx, ctx.actor, qid)
        elif action == "remove":
            questions.remove(tx, ctx.actor, qid)
        elif action == "edit":
            f = ctx.form
            questions.edit(tx, ctx.actor, qid, prompt=str(f.get("prompt", "")),
                           options=[str(o) for o in f_list(f.get("options[]") or f.get("options"))],
                           answer_index=f_int(f.get("answer_index"), "answer", required=False),
                           model_answer=str(f.get("model_answer", "")),
                           rubric_points=[p for p in str(f.get("rubric", "")).splitlines()], number=_number_fields(f))
        else:
            raise NotFound("Unknown action.")
    return redirect(f"/teacher/assignments/{aid}#q{qid}")


@page(T)
def add_own_question(ctx: Ctx) -> Response:
    aid, f = int(ctx.request.path_params["aid"]), ctx.form
    with ctx.tenant() as tx:
        qid = questions.add_own(tx, ctx.actor, aid, topic_id=f_int(f.get("topic_id"), "Topic"), kind=str(f.get("kind", "")),
                                prompt=str(f.get("prompt", "")), options=[str(o) for o in f_list(f.get("options[]"))],
                                answer_index=f_int(f.get("answer_index"), "answer", required=False),
                                model_answer=str(f.get("model_answer", "")),
                                rubric_points=str(f.get("rubric", "")).splitlines(), **_number_fields(f))
    return redirect(f"/teacher/assignments/{aid}#q{qid}", "Your question was added.")


def _number_fields(f: Any) -> dict[str, Any]:
    """A number problem's fields from the teacher's form."""
    return {"value": str(f.get("value", "")), "unit": str(f.get("unit", "")), "tolerance": str(f.get("tolerance", "")),
            "formula": str(f.get("formula", "")), "vars_text": str(f.get("vars", "")),
            "steps": str(f.get("steps", "")).splitlines(), "worked": str(f.get("worked", ""))}


@page(T)
def rewrite_basic(ctx: Ctx) -> Response:
    """Ask Claude again for a draft's Basic questions (the ones it couldn't write the first time)."""
    aid = int(ctx.request.path_params["aid"])
    n, why = questions.rewrite_basic(ctx.db, _engine(ctx), ctx.actor, aid)
    if n:
        return redirect(f"/teacher/assignments/{aid}#q-list", f"Claude rewrote {n} question{'s' if n != 1 else ''}. Check them below.")
    return redirect(f"/teacher/assignments/{aid}#q-list", "Claude still couldn't write them" + (f": {why}" if why else ".")
                    + " Try again in a minute, or edit or remove those questions.")


@page(T)
def remind_missing(ctx: Ctx) -> Response:
    """Remind the students who haven't handed in (teacher feedback: following up on missing work)."""
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        n = review.nudge_missing(tx, ctx.actor, aid)
    return redirect(f"/teacher/assignments/{aid}/review", f"Reminded {n} student{'s' if n != 1 else ''}." if n
                    else "Everyone has handed in.")


@page(T)
def approve_all(ctx: Ctx) -> Response:
    """Approve every question still waiting in a draft, in one click (UX-1)."""
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        a = assignments.get(tx, aid)
        require_class(tx, ctx.actor, a["class_id"])
        waiting = [q["id"] for q in questions.assignment_questions(tx, aid) if q["status"] == "draft"]
        for qid in waiting:
            questions.approve(tx, ctx.actor, qid)
    return redirect(f"/teacher/assignments/{aid}#sch-h", f"Approved {len(waiting)} question{'s' if len(waiting) != 1 else ''}. Now schedule it.")


@page(T)
def schedule(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    f = ctx.form
    targets: list[tuple[str, int]] = []
    who = str(f.get("who", "class"))
    with ctx.tenant() as tx:
        a = assignments.get(tx, aid)
        if who == "class":
            targets = [("class", a["class_id"])]
        elif who == "group":
            targets = [("group", int(g)) for g in f_list(f.get("groups[]"))]
        else:
            targets = [("student", int(s)) for s in f_list(f.get("students[]"))]
        ticked = [int(x) for x in f_list(f.get("sections[]")) if str(x).isdigit()]
        plan = []
        for cid in [a["class_id"]] + [c for c in ticked if c != a["class_id"]]:
            own = cid == a["class_id"]
            try:  # the draft's own class uses the main fields; each other section its own row
                opens = clock.parse_ist(str(f.get("opens_at") if own else f.get(f"opens_{cid}")))
                closes = clock.parse_ist(str(f.get("expires_at") if own else f.get(f"closes_{cid}")))
            except ValueError:
                raise ValidationError("Enter the opening and closing date and time for every class you ticked.") from None
            plan.append({"class_id": cid, "opens_at": opens, "expires_at": closes})
        ids = assignments.schedule_sections(tx, ctx.actor, aid, plan, targets=targets,
                                            time_limit_min=f_int(f.get("time_limit_min"), "Time limit", required=False),
                                            max_attempts=f_int(f.get("max_attempts") or 1, "Attempts"))
        labels = [r["label"] for r in tx.all("SELECT label FROM classes WHERE id = ANY(%s) ORDER BY grade, label", [[p["class_id"] for p in plan]])]
    if len(ids) > 1:
        return redirect("/teacher", f"Scheduled for {', '.join(labels)}. Each class sees it when it opens for them.")
    return redirect("/teacher", "Scheduled. Students will see it when it opens.")


@page(T)
def make_group(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        assignments.create_group(tx, ctx.actor, cid, str(ctx.form.get("name", "")), [int(s) for s in f_list(ctx.form.get("students[]"))])
    back = str(ctx.form.get("back", f"/teacher/classes/{cid}/students"))
    return redirect(back if back.startswith("/teacher/") else "/teacher", "Group created.")


@page(T)
def extend(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        sid = f_int(ctx.form.get("student_id"), "student", required=False)
        assignments.extend(tx, ctx.actor, aid, clock.parse_ist(str(ctx.form.get("expires_at"))), sid)
    return redirect(f"/teacher/assignments/{aid}/review", "Deadline extended.")


# ---------- exam papers (AS-8) ----------
@page(T)
def papers(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        items = exams.list_papers(tx, ctx.actor)
    return ctx.render("t_papers.html", nav="assignments", items=items, difficulties=exams.DIFFICULTIES)


@page(T)
def new_paper(ctx: Ctx) -> Response:
    f = ctx.form if ctx.request.method == "POST" else {}
    q = ctx.request.query_params
    with ctx.tenant() as tx:
        teaching = _teaching(ctx, tx)
        cur = _pick_teaching(teaching, str(f.get("for") or q.get("for") or ""))
        tree = syllabus.subject_tree(tx, cur["book"]) if cur else ()
        in_class = {p["chapter_id"] for p in plans.in_class_now(tx, cur["cid"]) if p["syllabus_id"] in cur["book"]["ids"]} if cur else set()
    chosen = {int(x) for x in f_list(f.get("topics[]")) if str(x).isdigit()}
    if not f:
        chosen = {t["id"] for ch in tree if ch["id"] in in_class for t in ch["topics"]}
    view = {"nav": "assignments", "teaching": teaching, "cur": cur, "tree": tree, "in_class": in_class, "chosen": chosen,
            "presets": exams.PRESETS, "kinds": exams.KINDS, "difficulties": exams.DIFFICULTIES, "f": f,
            "marks_choices": [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10]}
    if ctx.request.method == "GET":
        return ctx.render("t_paper_new.html", **view)
    try:
        if not cur:
            raise ValidationError("You aren't mapped to a class and subject yet. Ask your school admin.")
        preset = str(f.get("pattern", "1"))
        if preset.isdigit() and int(preset) < len(exams.PRESETS):
            rows = [(c, m, "") for c, m in exams.PRESETS[int(preset)][1]]
        else:
            rows = [(f.get(f"count_{i}", ""), f.get(f"marks_{i}", ""), f.get(f"kind_{i}", "")) for i in range(exams.MAX_SECTIONS)]
        pattern = exams.parse_pattern(rows, maths=books.is_maths(cur["book"].get("subject")))
        made = exams.create(ctx.db, _engine(ctx), ctx.actor, class_id=cur["cid"], syllabus_id=cur["book"]["id"],
                            title=str(f.get("title", "")), topic_ids=sorted(chosen), pattern=pattern,
                            difficulty=str(f.get("difficulty", "")), duration_min=f_int(f.get("duration") or 60, "Time allowed"),
                            instructions=str(f.get("instructions", "")), answer_space=bool(f.get("answer_space")))
    except ValidationError as exc:
        return ctx.render("t_paper_new.html", status=422, error=exc.message, **view)
    msg = "Three options are ready. Compare them and choose one."
    if made["shared"]:
        msg += (f" The sets share {made['shared']} question{'s' if made['shared'] != 1 else ''}: these topics couldn't give "
                "enough different ones. Choose more topics for more variety.")
    return redirect(f"/teacher/papers/{made['id']}", msg)


@page(T)
def paper_page(ctx: Ctx) -> Response:
    pid = int(ctx.request.path_params["pid"])
    with ctx.tenant() as tx:
        v = exams.view(tx, ctx.actor, pid)
    shown = f_int(ctx.request.query_params.get("set"), "Set", required=False) or v["paper"]["chosen_set"] or 1
    return ctx.render("t_paper.html", nav="assignments", shown=max(1, min(3, shown)), **v)


@page(T)
def paper_action(ctx: Ctx) -> Response:
    pid, action = int(ctx.request.path_params["pid"]), ctx.request.path_params["action"]
    set_no = f_int(ctx.form.get("set"), "Set")
    if action == "choose":
        with ctx.tenant() as tx:
            exams.choose(tx, ctx.actor, pid, set_no)
        return redirect(f"/teacher/papers/{pid}?set={set_no}", f"You chose Set {exams.SET_NAMES[set_no - 1]}. Download it below to print.")
    if action == "replace":
        sec, item = f_int(ctx.form.get("section"), "Section"), f_int(ctx.form.get("item"), "Question")
        exams.replace(ctx.db, _engine(ctx), ctx.actor, pid, set_no, sec, item)
        return redirect(f"/teacher/papers/{pid}?set={set_no}#s{sec}-{item}", "Question replaced.")
    raise NotFound("Unknown action.")


@page(T)
def paper_pdf(ctx: Ctx) -> Response:
    pid, set_no = int(ctx.request.path_params["pid"]), int(ctx.request.path_params["n"])
    key = ctx.request.path_params.get("kind") == "key"
    with ctx.tenant() as tx:
        v = exams.view(tx, ctx.actor, pid)
        school = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
    if set_no not in (1, 2, 3):
        raise NotFound("That set doesn't exist.")
    pdf = pdfs.exam_paper(v, set_no, school, key=key)
    name = f"{v['paper']['title']}-set-{exams.SET_NAMES[set_no - 1]}{'-answer-key' if key else ''}"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name).strip("-").lower()[:80] or "exam"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={safe}.pdf"})


# ---------- learning growth (1.16.0) ----------
@page(*STAFF)
def class_growth(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        require_class(tx, ctx.actor, cid)
        label = tx.val("SELECT label FROM classes WHERE id=%s", [cid])
        n = tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [cid])
        books_ = syllabus.class_subjects(tx, cid)
        book = syllabus.pick(books_, f_int(ctx.request.query_params.get("book"), "book", required=False))
        data = growth.report(tx, ctx.actor, cid, book["id"]) if book else None
    if ctx.request.query_params.get("format") == "pdf" and data:
        rows = [[f"{s['roll_no']}. {s['name']}", s["baseline"], s["latest"], "" if s["change"] is None else f"{s['change']:+d}"]
                for s in data["students"]]
        rows += [[(f"Part {c['part']} · " if c["part"] else "") + f"Ch {c['code']} {c['title']}", c["baseline"], c["latest"], "" if c["change"] is None else f"{c['change']:+d}"]
                 for c in data["chapters"]]
        sm = data["summary"]
        sub = (f"Class average {sm['baseline']}% → {sm['latest']}% ({sm['change']:+d} points), {sm['n']} students who took both."
               if sm else "Growth shows after the first progress check.")
        pdf = pdfs.table_report(f"Class {label}: learning growth in {book['subject']}", sub,
                                ["Student / chapter", "Start of term %", "Latest %", "Change"], rows)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=growth-{label}.pdf"})
    return ctx.render("t_growth.html", nav="progress", cid=cid, label=label, students_n=n, books=books_, book=book, g=data)


@page(T)
def new_growth_check(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    aid = growth.create_check(ctx.db, _engine(ctx), ctx.actor, cid, f_int(ctx.form.get("book"), "Subject"),
                              str(ctx.form.get("purpose", "")))
    return redirect(f"/teacher/assignments/{aid}", "Check drafted. Look over the questions, then schedule it for the class.")


# ---------- paper work (1.21.0) ----------
def _paper_root(ctx: Ctx):
    return paper.storage_dir(ctx.settings.data_dir)


@page(T, max_files=paper.MAX_PAGES)
def paperwork_home(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    claude = getattr(_engine(ctx), "name", "") == "claude"
    if ctx.request.method == "POST":
        f = ctx.form
        ups = f.get("files[]") or []
        ups = ups if isinstance(ups, list) else [ups]
        paper.upload(ctx.db, ctx.actor, aid, f_int(f.get("student_id"), "Student"),
                     [(u.filename, u.file) for u in ups if getattr(u, "filename", "")], _paper_root(ctx), claude=claude)
        return redirect(f"/teacher/assignments/{aid}/paper",
                        "Photos uploaded. Claude is reading them; check back in a minute." if claude else
                        "Photos uploaded. Open “Check and mark” to type the answers.")
    with ctx.tenant() as tx:
        p = paper.overview(tx, ctx.actor, aid)
    return ctx.render("t_paperwork.html", nav="assignments", p=p, claude=claude)


@page(T)
def paperwork_scan(ctx: Ctx) -> Response:
    sid = int(ctx.request.path_params["sid"])
    if ctx.request.method == "POST":
        paper.check_and_mark(ctx.db, _engine(ctx), ctx.actor, sid, {k: str(v) for k, v in ctx.form.items()})
        with ctx.tenant() as tx:
            aid = tx.val("SELECT assignment_id FROM paper_scans WHERE id=%s", [sid])
        return redirect(f"/teacher/assignments/{aid}/paper", "Marked. It's in the assignment's results for you to review.")
    with ctx.tenant() as tx:
        v = paper.scan_view(tx, ctx.actor, sid)
    return ctx.render("t_paperwork_scan.html", nav="assignments", v=v)


@page(T)
def paperwork_photo(ctx: Ctx) -> Response:
    sid, n = int(ctx.request.path_params["sid"]), int(ctx.request.path_params["n"])
    with ctx.tenant() as tx:
        v = paper.scan_view(tx, ctx.actor, sid)
    files = paper.pages(_paper_root(ctx), ctx.actor.institution_id, v["scan"]["assignment_id"], v["scan"]["student_id"])
    if not 1 <= n <= len(files):
        raise NotFound("That page wasn't found.")
    p = files[n - 1]
    return Response(p.read_bytes(), media_type={"png": "image/png", "webp": "image/webp"}.get(p.suffix.lstrip("."), "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=600"})


# ---------- weekly class briefing (1.18.0) ----------
@page(*STAFF)
def class_briefing(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        b = briefing.weekly(tx, ctx.actor, cid)
        label = tx.val("SELECT label FROM classes WHERE id=%s", [cid])
        n = tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [cid])
    if ctx.request.query_params.get("format") == "pdf":
        rows = [["Mix-up", f"{m['label']} ({m['topic']})", f"{m['students']} students"] for m in b["mixups"]]
        rows += [["Hard topic", f"{h['chapter']}. {h['title']}", f"{h['pct']}%"] for h in b["hard"]]
        rows += [["Check on", f"{s['roll_no']}. {s['name']}", "; ".join(s["reasons"])] for s in b["slipping"]]
        if b["focus"]:
            rows += [[f"Reteach {s['min']} min", s["name"], s["text"] + (f" Q: {s['question']} Answer: {s['options'][s['answer']]}"
                      if s.get("question") else "")] for s in b["focus"]["steps"]]
        _, body = briefing.text(label, b)
        pdf = pdfs.table_report(f"Class {label}: weekly briefing, {b['from'].strftime('%d %b')} – {b['to'].strftime('%d %b %Y')}",
                                body, ["", "What", "Detail"], rows)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=briefing-{label}.pdf"})
    return ctx.render("t_briefing.html", nav="progress", cid=cid, label=label, students_n=n, b=b)


# ---------- escape rooms (LN-12, DB-8) ----------
@page(*STAFF)
def class_escape(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        data = escape.class_view(tx, ctx.actor, cid)
        label = tx.val("SELECT label FROM classes WHERE id=%s", [cid])
    return ctx.render("t_escape.html", nav="progress", cid=cid, label=label, students_n=len(data["students"]), **data)


@page(*STAFF)
def escape_guide(ctx: Ctx) -> Response:
    book = f_int(ctx.request.query_params.get("book"), "book", required=False)
    with ctx.tenant() as tx:
        if book is None:  # an old link (built-in NCERT rooms)
            book = tx.val("""SELECT id FROM syllabi WHERE institution_id IS NULL AND board='NCERT' AND subject='Science'
                             AND grade='8' ORDER BY is_current DESC, id DESC LIMIT 1""")
            if book is None:
                raise NotFound("Open the answer guide from a class's escape rooms page.")
        data = escape.guide(tx, ctx.actor, int(ctx.request.path_params["ch"]), book)
    return ctx.render("t_escape_guide.html", nav="syllabus", **data)


# ---------- term plan (teacher review, item 12) ----------
@page(T)
def term_plan(ctx: Ctx) -> Response:
    f = ctx.form if ctx.request.method == "POST" else {}
    with ctx.tenant() as tx:
        teaching = _teaching(ctx, tx)
        cur = _pick_teaching(teaching, str(f.get("for") or ctx.request.query_params.get("for") or ""),
                             f_int(ctx.request.query_params.get("class"), "Class", required=False))
        if not cur:
            return ctx.render("t_plan.html", nav="plan", teaching=teaching, cur=None, plan=None)
        error = None
        if ctx.request.method == "POST":
            try:
                return _save_plan(tx, ctx, cur, f)
            except ValidationError as exc:
                error = exc.message
        plan = plans.view(tx, ctx.actor, cur["cid"], cur["book"]["id"])
        sections_ = today.sections(teaching, cur)
        notes = {t["key"]: plans.section_status(tx, ctx.actor, t["cid"], t["book"]["id"]) for t in sections_}
    return ctx.render("t_plan.html", nav="plan", teaching=teaching, cur=cur, plan=plan, error=error, f=f,
                      section_notes=notes, others=[t for t in sections_ if t["key"] != cur["key"]], status=422 if error else 200)


def _save_plan(tx, ctx: Ctx, cur: dict, f) -> Response:
    """Save typed dates, or spread chapters over the term; ValidationError leaves the plan untouched."""
    cid, sid = cur["cid"], cur["book"]["id"]
    if f.get("action") == "copy":
        targets = [int(x) for x in f_list(f.get("classes[]")) if str(x).isdigit()]
        if not targets:
            raise ValidationError("Tick the classes to copy the dates to.")
        n = plans.copy_to(tx, ctx.actor, cid, sid, targets)
        return redirect(f"/teacher/plan?for={cur['key']}", f"Copied {cur['class_label']}'s dates to {n} class{'es' if n != 1 else ''}.")
    if f.get("action") == "fill":
        chosen = [int(x) for x in f_list(f.get("chapters[]")) if str(x).isdigit()]
        plans.autofill(tx, ctx.actor, cid, sid, plans.parse_day(f.get("term_start"), "First day of term"),
                           plans.parse_day(f.get("term_end"), "Last day of term"), chosen)
        what = "every chapter" if not chosen else f"{len(chosen)} chapter{'s' if len(chosen) != 1 else ''}"
        msg = f"Spread {what} over the term. Change any dates below."
    else:
        ids = [int(x) for x in f_list(f.get("chapter[]")) if str(x).isdigit()]
        code = {ch["id"]: (f"P{ch['part']}·" if ch["part"] else "") + str(ch["code"]) for ch in syllabus.subject_tree(tx, cur["book"])}
        entries = [(i, plans.parse_day(f.get(f"start_{i}"), f"Start of chapter {code.get(i, '')}"),
                    plans.parse_day(f.get(f"end_{i}"), f"End of chapter {code.get(i, '')}")) for i in ids]
        n = plans.save(tx, ctx.actor, cid, sid, entries)
        msg = f"Term plan saved: {n} chapter{'s' if n != 1 else ''} dated."
    return redirect(f"/teacher/plan?for={cur['key']}", msg)


# ---------- timetable (1.30.0) ----------
@page(T)
def timetable_page(ctx: Ctx) -> Response:
    f = ctx.form if ctx.request.method == "POST" else {}
    error = None
    with ctx.tenant() as tx:
        teaching = _teaching(ctx, tx)
        if ctx.request.method == "POST":
            try:
                if f.get("action") == "remove":
                    timetable.remove(tx, ctx.actor, f_int(f.get("slot"), "Lesson"))
                    return redirect("/teacher/timetable", "Lesson removed from your timetable.")
                cur = _pick_teaching(teaching, str(f.get("for") or ""))
                if not cur or cur["key"] != str(f.get("for")):
                    raise ValidationError("Choose the class and subject.")
                n = timetable.add(tx, ctx.actor, cur["cid"], cur["book"]["id"],
                                  [int(d) for d in f_list(f.get("days[]")) if str(d).isdigit()],
                                  timetable.parse_time(str(f.get("starts_at", ""))), f_int(f.get("minutes") or 40, "Length"))
                return redirect("/teacher/timetable", f"Added {n} lesson{'s' if n != 1 else ''} for {cur['short']}.")
            except ValidationError as exc:
                error = exc.message
        rows = timetable.slots(tx, ctx.actor.user_id)
        per_week = {}
        for r in rows:
            per_week[r["key"]] = per_week.get(r["key"], 0) + 1
    return ctx.render("t_timetable.html", nav="timetable", teaching=teaching, cur=None, rows=rows, grid=timetable.week_grid(rows),
                      days=timetable.DAY_NAMES, lengths=timetable.LENGTHS, per_week=per_week, error=error, f=f,
                      status=422 if error else 200)


# ---------- paper and projector (teacher review, item 11) ----------
@page(*STAFF)
def worksheet(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = worksheets.sheet(tx, ctx.actor, int(ctx.request.path_params["aid"]))
    return ctx.render("t_worksheet.html", key=ctx.request.query_params.get("key") == "1", **data)


@page(*STAFF)
def present(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        data = worksheets.sheet(tx, ctx.actor, aid)
        results = worksheets.class_results(tx, ctx.actor, aid)
    return ctx.render("t_present.html", results=results, **data)


# ---------- results review ----------
@page(*STAFF)
def review_page(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        data = review.queue(tx, ctx.actor, aid)
        results = dashboards.assignment_results(tx, ctx.actor, aid)
        sessions = [s for s in teach.sessions_for_class(tx, ctx.actor, data["assignment"]["class_id"])] \
            if data["assignment"]["kind"] == "teach" else []
        tab = ctx.request.query_params.get("tab", "check")
        sibs = assignments.siblings(tx, assignments.get(tx, aid))
        feelings = myprogress.class_feelings(tx, aid)
        breakdown = review.question_breakdown(tx, ctx.actor, aid) if tab == "questions" else None
    now_ist = clock.to_ist(clock.now())
    return ctx.render("t_review.html", nav="review", tab=tab, results=results, sessions=sessions, kinds=questions.KINDS,
                      breakdown=breakdown, siblings=sibs, feelings=feelings,
                      default_extend=(now_ist + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), **data)


@page(T)
def override(ctx: Ctx) -> Response:
    rid = int(ctx.request.path_params["rid"])
    raw = ctx.form.get("score")
    score = None if raw in (None, "", "ai") else float(raw)
    with ctx.tenant() as tx:
        aid = tx.val("SELECT t.assignment_id FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE r.id=%s", [rid])
        review.override(tx, ctx.actor, rid, score=score, comment=str(ctx.form.get("comment", "")))
    return redirect(f"/teacher/assignments/{aid}/review", "Saved. Progress updated.")


@page(T)
def feedback(ctx: Ctx) -> Response:
    att = int(ctx.request.path_params["att"])
    with ctx.tenant() as tx:
        review.add_feedback(tx, ctx.actor, att, str(ctx.form.get("feedback", "")))
        row = tx.one("SELECT assignment_id, student_id FROM attempts WHERE id=%s", [att])
    return redirect(f"/teacher/assignments/{row['assignment_id']}/students/{row['student_id']}", "Feedback saved.")


@page(T)
def suggest_note(ctx: Ctx) -> Response:
    """A note to one student drafted from their results (Claude, else NovaQuest's own wording), put in the feedback
    box for the teacher to edit; nothing is saved or sent until the teacher saves it."""
    att = int(ctx.request.path_params["att"])
    with ctx.tenant() as tx:
        row = tx.one("SELECT assignment_id, student_id FROM attempts WHERE id=%s", [att])
        if not row:
            raise NotFound("That attempt was not found.")
        card = review.scorecard(tx, ctx.actor, row["assignment_id"], row["student_id"])
        student = tx.one("SELECT id, name FROM users WHERE id=%s", [row["student_id"]])
    info = {"first_name": student["name"].split()[0], "title": card["assignment"]["title"], "right": card["right"],
            "total": card["total"], "mixups": card["mixups"][:3],
            "topics": [{"name": c["name"], "pct": c["pct"]} for c in card["concepts"] if not c["ai"]]}
    engine = get_engine(ctx.settings, ctx.db, ctx.actor.institution_id)
    note = engine.suggest_note(info)
    return ctx.render("t_scorecard.html", nav="review", card=card, student=student, attempt_id=att, suggested=note,
                      by_claude=getattr(engine, "name", "") == "claude")


@page(T)
def release(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        n = review.release(tx, ctx.actor, aid, str(ctx.form.get("message", "")))
    return redirect(f"/teacher/assignments/{aid}/review", f"Results released to {n} students.")


@page(T)
def class_message(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        review.set_class_message(tx, ctx.actor, aid, str(ctx.form.get("message", "")))
    return redirect(f"/teacher/assignments/{aid}/review", "Message to the class saved.")


@page(*STAFF)
def student_scorecard(ctx: Ctx) -> Response:
    aid, sid = int(ctx.request.path_params["aid"]), int(ctx.request.path_params["sid"])
    with ctx.tenant() as tx:
        card = review.scorecard(tx, ctx.actor, aid, sid)
        student = tx.one("SELECT id, name FROM users WHERE id=%s", [sid])
        att = tx.one("SELECT id FROM attempts WHERE assignment_id=%s AND student_id=%s AND status='submitted' ORDER BY attempt_no DESC LIMIT 1",
                     [aid, sid])
    return ctx.render("t_scorecard.html", nav="review", card=card, student=student, attempt_id=att["id"] if att else None)


# ---------- syllabus board ----------
@page(*STAFF)
def picture_preview(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = pictures.preview(tx, ctx.actor, int(ctx.request.path_params["tid"]))
    return ctx.render("t_pictures.html", nav="syllabus", **data)


@page(*STAFF)
def staff_book_pages(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        view = pages.page_view(tx, tid)
    return ctx.render("pages.html", nav="books" if ctx.actor.role == "inst_admin" else "syllabus",
                      base=f"/teacher/syllabus/topics/{tid}", layout="staff.html", **view)


@page(*STAFF)
def staff_book_page_image(ctx: Ctx) -> Response:
    tid, n = int(ctx.request.path_params["tid"]), int(ctx.request.path_params["n"])
    with ctx.tenant() as tx:
        img = pages.render(tx, tid, n, ctx.settings.data_dir)
    return FileResponse(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@page(*STAFF)
def syllabus_board(ctx: Ctx) -> Response:
    q = ctx.request.query_params
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
        teaching = _teaching(ctx, tx)
        open_topic = f_int(q.get("topic"), "topic", required=False)
        cur = _pick_teaching(teaching, q.get("for"), f_int(q.get("class"), "class", required=False),
                             syllabus.topic_syllabus_id(tx, open_topic) if open_topic else None)
        if not cur:
            return ctx.render("error.html", status=404, message="You aren't mapped to a class and subject yet. Ask your school admin.")
        cid = cur["cid"]
        require_class(tx, ctx.actor, cid)
        tree = syllabus.subject_tree(tx, cur["book"])
        assigned = {t for r in tx.all("SELECT topic_ids FROM assignments WHERE class_id=%s AND state <> 'draft'", [cid]) for t in r["topic_ids"]}
        locked = learn.closed_topics(tx, cid)
        mode = learn.self_study_mode(tx, cid, cur["book"]["id"])
        starts = {r["chapter_id"]: r["starts_on"] for r in tx.all("SELECT chapter_id, starts_on FROM term_plans WHERE class_id=%s", [cid])}
        grid: dict[int, dict[str, int]] = {}
        for r in mastery.for_class(tx, cid):
            if r["topic_id"]:
                g = grid.setdefault(r["topic_id"], {"0": 0, "1": 0, "2": 0, "3": 0})
                g[str(r["level"])] += 1
        counts = {r["node_id"]: r["n"] for r in tx.all("""SELECT node_id, count(*) n FROM source_passages GROUP BY node_id""")}
        notes = {r["node_id"]: r["n"] for r in tx.all("""SELECT node_id, count(*) n FROM source_passages WHERE institution_id=%s
                                                         GROUP BY node_id""", [tx.inst])}
        n_students = tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [cid])
        detail = None
        if open_topic:
            detail = {"topic": syllabus.get_topic(tx, open_topic), "passages": syllabus.topic_passages(tx, open_topic, 12),
                      "students": [r for r in mastery.for_class(tx, cid, [open_topic])]}
            detail["has_pictures"] = pictures.has_lesson_for(tx, detail["topic"])
            detail["pictures_done"] = pictures.class_progress(tx, cid).get(open_topic, 0)
    return ctx.render("t_syllabus.html", nav="syllabus", classes=classes, teaching=teaching, cur=cur, cid=cid, tree=tree,
                      assigned=assigned, locked=locked, mode=mode, modes=learn.MODES, starts=starts, today=clock.to_ist(clock.now()).date(),
                      grid=grid, counts=counts, notes=notes, n_students=n_students, detail=detail)


# ---------- unit prep packs (1.26.0) ----------
def _prep_chapter(tx, ctx: Ctx, cur: dict, chapter_id: int | None) -> tuple[list[dict], dict | None]:
    """(the subject's chapters, the chosen one): asked for, else the chapter in class now, else the next planned one,
    else the first."""
    tree = syllabus.subject_tree(tx, cur["book"])
    chosen = next((c for c in tree if c["id"] == chapter_id), None)
    if chosen is None:
        now = [p["chapter_id"] for p in plans.in_class_now(tx, cur["cid"])]
        nxt = tx.all("""SELECT chapter_id FROM term_plans WHERE class_id=%s AND starts_on > %s ORDER BY starts_on""",
                     [cur["cid"], clock.now().date()])
        for cid in now + [r["chapter_id"] for r in nxt]:
            chosen = next((c for c in tree if c["id"] == cid), None)
            if chosen:
                break
    return tree, chosen or (tree[0] if tree else None)


@page(*STAFF)
def unit_prep(ctx: Ctx) -> Response:
    q = ctx.request.query_params
    with ctx.tenant() as tx:
        teaching = _teaching(ctx, tx)
        chapter_id = f_int(q.get("chapter"), "chapter", required=False)
        cur = _pick_teaching(teaching, q.get("for"), f_int(q.get("class"), "class", required=False),
                             tx.val("SELECT syllabus_id FROM syllabus_nodes WHERE id=%s", [chapter_id]) if chapter_id else None)
        if not cur:
            return ctx.render("error.html", status=404, message="You aren't mapped to a class and subject yet. Ask your school admin.")
        require_class(tx, ctx.actor, cur["cid"])
        tree, ch = _prep_chapter(tx, ctx, cur, chapter_id)
        if not ch:
            return ctx.render("error.html", status=404, message="This book has no chapters yet.")
        claude = getattr(_engine(ctx), "name", "") == "claude"
        if claude and ctx.actor.role == "teacher" and prep.stored(tx, ch["id"]) is None:
            prep.request_claude(tx, ctx.actor, ch["id"])  # first visit: Claude writes the fuller pack in the background
        data, row = prep.pack(tx, ch["id"])
        periods = f_int(q.get("periods"), "Periods", required=False) or prep.default_periods(tx, cur["cid"], ch["id"], data)
        periods = max(1, min(periods, 30))
        minutes = f_int(q.get("minutes"), "Minutes", required=False) or 40
        minutes = minutes if minutes in prep.PERIOD_MINUTES else 40
        dates = tx.one("SELECT starts_on, ends_on FROM term_plans WHERE class_id=%s AND chapter_id=%s", [cur["cid"], ch["id"]])
        notes = prep.notes(tx, ctx.actor, ch["id"])
        seen = prep.seen_in_class(tx, cur["cid"], ch["id"])
        book = syllabus.get_syllabus(tx, ch["syllabus_id"])
        pages_ok = figures.allowed(book)
        figs = {t["id"]: figures.topic_figures(tx, t["id"], ctx.settings.data_dir, read=False) for t in data["topics"]} \
            if pages_ok and q.get("format") != "pdf" else {}
        figs_pending = pages_ok and not all(figures.ready(tx, t["id"], ctx.settings.data_dir) for t in data["topics"])
        if figs_pending:
            figures.queue_warm(tx, ch["syllabus_id"])
        pos = prep.position(tx, cur["cid"], ch["id"], periods)
        section_notes, elsewhere = {}, {}
        for t in today.sections(teaching, cur):  # every section of this subject: where it is in this chapter
            p_t = pos if t["key"] == cur["key"] else prep.position(tx, t["cid"], ch["id"], periods)
            section_notes[t["key"]] = ("Not in its term plan" if not p_t else f"Starts {p_t['starts_on']:%d %b}" if not p_t["started"]
                                       else "All periods taught" if p_t["done"] else f"Period {p_t['period']} next")
            if p_t and p_t["started"] and not p_t["done"] and t["key"] != cur["key"]:
                elsewhere.setdefault(p_t["period"], []).append(t["class_label"])
        timetabled = timetable.lessons_between(tx, cur["cid"], cur["book"]["ids"], plans.today(), plans.today()) is not None
    view = {"teaching": teaching, "cur": cur, "pos": pos, "section_notes": section_notes, "elsewhere": elsewhere, "timetabled": timetabled, "tree": tree, "ch": ch, "p": data, "row": row, "claude": claude,
            "periods": periods, "minutes": minutes, "lessons": prep.arrange(data, periods, minutes), "dates": dates,
            "notes": notes, "seen": seen, "periods_chosen": bool(q.get("periods")), "figs": figs, "pages_ok": pages_ok, "figs_pending": figs_pending, "acts": {a["ref"]: a for a in data["activities"] if a.get("ref")}}
    if q.get("format") == "pdf":
        pdf = pdfs.unit_pack(view)
        name = f"prep-ch{ch['code']}" + (f"-part{ch['part']}" if ch.get("part") else "")
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    return ctx.render("t_prep.html", nav="syllabus", **view)


@page(*STAFF)
def chapter_deck(ctx: Ctx) -> Response:
    """Teach a chapter from the projector, one period at a time, inside the app (no download). `view=presenter` is
    the teacher's own screen: notes, the period's timer and the next slide, kept in step with the projector window."""
    q, chapter_id = ctx.request.query_params, int(ctx.request.path_params["ch"])
    with ctx.tenant() as tx:
        teaching = _teaching(ctx, tx)
        sid = tx.val("SELECT syllabus_id FROM syllabus_nodes WHERE id=%s AND kind='chapter'", [chapter_id])
        if not sid:
            raise NotFound("That chapter was not found.")
        cur = _pick_teaching(teaching, q.get("for"), f_int(q.get("class"), "class", required=False), sid)
        if not cur or sid not in cur["book"]["ids"]:
            raise NotFound("That chapter isn't in a subject you teach.")
        require_class(tx, ctx.actor, cur["cid"])
        data, _ = prep.pack(tx, chapter_id)
        periods = f_int(q.get("periods"), "Periods", required=False) or prep.default_periods(tx, cur["cid"], chapter_id, data)
        periods = max(1, min(periods, 30))
        minutes = f_int(q.get("minutes"), "Minutes", required=False) or 40
        minutes = minutes if minutes in prep.PERIOD_MINUTES else 40
        lessons = prep.arrange(data, periods, minutes)
        if not lessons:
            return ctx.render("error.html", status=404, message="This chapter has no topics yet.")
        no = max(1, min(f_int(q.get("period"), "Period", required=False) or 1, len(lessons)))
        period = lessons[no - 1]
        deck = decks.slides(tx, chapter_id, data, period, ctx.settings.data_dir)
        book = syllabus.get_syllabus(tx, sid)
        if figures.allowed(book) and not all(figures.ready(tx, t["id"], ctx.settings.data_dir) for t in period["topics"]):
            figures.queue_warm(tx, sid)
        school = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
        teacher = tx.val("SELECT name FROM users WHERE id=%s", [ctx.actor.user_id])
        presenter = q.get("view") == "presenter"
        if not presenter:
            decks.record(tx, ctx.actor, chapter_id, no)
    base = f"/teacher/present/{chapter_id}?for={cur['key']}&periods={periods}&minutes={minutes}"
    resp = ctx.render("t_deck.html", deck=deck, period=period, lessons=lessons, no=no, base=base, minutes=minutes,
                      cur=cur, chapter_id=chapter_id, presenter=presenter,
                      watermark=f"{school} · {teacher} · {clock.to_ist(clock.now()):%d %b %Y}",
                      channel=f"nq-deck-{ctx.actor.user_id}-{chapter_id}-{no}")
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Robots-Tag"] = "noindex, noarchive"
    return resp


@page(*STAFF)
def deck_figure_image(ctx: Ctx) -> Response:
    """One of the book's figures on a slide (or the prep page): only to signed-in staff, never kept by the browser."""
    chapter_id, tid, n, k = (int(ctx.request.path_params[x]) for x in ("ch", "tid", "n", "k"))
    with ctx.tenant() as tx:
        if tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s AND kind='topic'", [tid]) != chapter_id:
            raise NotFound("That figure isn't available.")
        img = figures.image(tx, tid, n, k, ctx.settings.data_dir)
    return FileResponse(img, media_type="image/jpeg", headers={"Cache-Control": "no-store, private", "Content-Disposition": "inline"})


@page(*STAFF)
def deck_page_image(ctx: Ctx) -> Response:
    """A textbook page shown on a slide: only to signed-in staff, and never kept by the browser."""
    chapter_id, tid, n = (int(ctx.request.path_params[k]) for k in ("ch", "tid", "n"))
    with ctx.tenant() as tx:
        if tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s AND kind='topic'", [tid]) != chapter_id:
            raise NotFound("That page isn't available.")
        img = pages.render(tx, tid, n, ctx.settings.data_dir)
    return FileResponse(img, media_type="image/jpeg", headers={"Cache-Control": "no-store, private",
                                                                "Content-Disposition": "inline"})


@page(*STAFF)
def unit_prep_action(ctx: Ctx) -> Response:
    chapter_id, action, f = int(ctx.request.path_params["ch"]), ctx.request.path_params["action"], ctx.form
    key = str(f.get("for", ""))
    back = f"/teacher/prep?for={key}&chapter={chapter_id}"
    if action == "quiz":
        cid = f_int(key.partition(":")[0], "Class")
        aid = prep.opening_quiz(ctx.db, _engine(ctx), ctx.actor, cid, chapter_id)
        return redirect(f"/teacher/assignments/{aid}", "Opening quiz drafted. Check the questions, then schedule it or "
                        "show it on the projector.")
    with ctx.tenant() as tx:
        if action == "claude":
            if getattr(_engine(ctx), "name", "") != "claude":
                raise ValidationError("Claude isn't set up on this NovaQuest (no API key), or today's AI limit was reached.")
            prep.request_claude(tx, ctx.actor, chapter_id)
            msg = "Claude is writing the pack. It takes a minute or two; this page shows the book's pack meanwhile."
        elif action == "notes":
            prep.save_notes(tx, ctx.actor, chapter_id, str(f.get("notes", "")))
            msg = "Notes saved."
        else:
            raise ValidationError("Unknown action.")
    return redirect(back, msg)


@page(T)
def chapters_open(ctx: Ctx) -> Response:
    """Open or lock whole chapters for a class, open only the ticked ones, or set how the book starts (1.28.0)."""
    f = ctx.form
    key = str(f.get("for", ""))
    with ctx.tenant() as tx:
        cur = _pick_teaching(_teaching(ctx, tx), key)
        if not cur or cur["key"] != key:
            raise NotFound("That class and subject were not found.")
        action = str(f.get("action", ""))
        if action in ("open", "lock"):
            ch = f_int(f.get("chapter_id"), "Chapter")
            learn.set_chapters_open(tx, ctx.actor, cur["cid"], [ch], action == "open")
            msg = "Chapter opened for self-study." if action == "open" else "Chapter locked: students don't see it on Learn."
        elif action == "only":
            chosen = [int(x) for x in f_list(f.get("chapters[]") or f.get("chapters")) if str(x).isdigit()]
            learn.open_only(tx, ctx.actor, cur["cid"], cur["book"]["ids"], chosen)
            msg = f"Opened {len(chosen)} chapter{'s' if len(chosen) != 1 else ''}; the rest are locked."
        elif action == "mode":
            learn.set_self_study_mode(tx, ctx.actor, cur["cid"], cur["book"]["ids"], str(f.get("mode", "")))
            msg = "Saved. " + learn.MODES[str(f.get("mode"))] + "."
        else:
            raise ValidationError("Unknown action.")
    return redirect(f"/teacher/syllabus?for={key}", msg)


@page(T)
def topic_open(ctx: Ctx) -> Response:
    tid, cid = int(ctx.request.path_params["tid"]), f_int(ctx.form.get("class_id"), "class")
    with ctx.tenant() as tx:
        learn.set_topic_open(tx, ctx.actor, cid, tid, ctx.form.get("open") == "1")
    return redirect(f"/teacher/syllabus?class={cid}#t{tid}")


@page(T)
def topic_notes(ctx: Ctx) -> Response:
    tid, cid = int(ctx.request.path_params["tid"]), f_int(ctx.form.get("class_id"), "class")
    upload = ctx.form.get("file")
    text = str(ctx.form.get("text", ""))
    if upload is not None and getattr(upload, "filename", ""):
        data = upload.file.read(ctx.settings.upload_max_bytes + 1)
        if len(data) > ctx.settings.upload_max_bytes:
            raise ValidationError("That file is larger than 10 MB.")
        text = syllabus.extract_upload_text(upload.filename, data, ctx.settings.data_dir / "tmp")
    with ctx.tenant() as tx:
        n = syllabus.add_teacher_notes(tx, tid, text, title=str(ctx.form.get("title", "Notes")), actor_id=ctx.actor.user_id)
    return redirect(f"/teacher/syllabus?class={cid}&topic={tid}#detail", f"Added {n} passage{'s' if n != 1 else ''} as AI source material.")


# ---------- students ----------
@page(T, "inst_admin")
def class_students(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        require_class(tx, ctx.actor, cid)
        klass = tx.one("SELECT id, label FROM classes WHERE id=%s", [cid])
        rows = students.list_students(tx, cid)
        groups = assignments.list_groups(tx, cid)
        others = tx.all("SELECT id, label FROM classes WHERE status='active' AND id<>%s ORDER BY label", [cid])
        last = tx.val("SELECT max(report_emailed_at) FROM enrolments WHERE class_id=%s", [cid])
    return ctx.render("t_students.html", nav="students", klass=klass, rows=rows, groups=groups, others=others, check=None,
                      last_emailed=last)


@page(T, "inst_admin")
def parent_reports(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    with ctx.tenant() as tx:
        r = reports.email_parents(tx, ctx.actor, cid, ctx.settings.base_url)
    skipped = [f"{r['no_consent']} without consent recorded" if r["no_consent"] else "",
               f"{r['no_email']} without a parent email" if r["no_email"] else ""]
    skipped = [x for x in skipped if x]
    return redirect(f"/teacher/classes/{cid}/students", f"Progress reports emailed to {r['sent']} parent{'s' if r['sent'] != 1 else ''}."
                    + (f" Skipped: {'; '.join(skipped)}." if skipped else ""))


@page(T, "inst_admin")
def add_student(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    f = ctx.form
    with ctx.tenant() as tx:
        added = students.add_student(tx, ctx.actor, class_id=cid, name=str(f.get("name", "")), roll_no=str(f.get("roll_no", "")),
                                     parent_email=str(f.get("parent_email", "")), consent_by=str(f.get("consent_by", "")),
                                     consent_method=str(f.get("consent_method", "")))
    return _slips(ctx, cid, [added])


@page(T, "inst_admin")
def import_students(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    f = ctx.form
    upload = f.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        data = upload.file.read(ctx.settings.upload_max_bytes + 1)
        if len(data) > ctx.settings.upload_max_bytes:
            raise ValidationError("That file is larger than 10 MB.")
        rows = students.parse_rows(data, upload.filename)
    else:
        rows = [{"roll_no": r, "name": n, "parent_email": e} for r, n, e in
                zip(f_list(f.get("roll_no[]")), f_list(f.get("name[]")), f_list(f.get("parent_email[]")))]
    with ctx.tenant() as tx:
        require_class(tx, ctx.actor, cid, write=True)
        klass = tx.one("SELECT id, label FROM classes WHERE id=%s", [cid])
        if f.get("commit") in ("all", "skip"):
            res = students.import_rows(tx, ctx.actor, cid, rows, skip_invalid=f.get("commit") == "skip")
            added = res.added
        else:
            check = students.check_rows(tx, cid, rows)
            return ctx.render("t_students.html", nav="students", klass=klass, rows=students.list_students(tx, cid),
                              groups=assignments.list_groups(tx, cid), others=[], check=check,
                              filename=getattr(upload, "filename", "") or "edited rows")
    return _slips(ctx, cid, added)


def _slips(ctx: Ctx, cid: int, added: list[dict]) -> Response:
    with ctx.tenant() as tx:
        klass = tx.one("SELECT label FROM classes WHERE id=%s", [cid])
        code = tx.val("SELECT school_code FROM institutions WHERE id=%s", [tx.inst])
    slips = [{"name": a["name"], "roll_no": a["roll_no"], "pin": a["pin"]} for a in added]
    token = _slips_signer(ctx).dumps({"c": cid, "s": slips})
    return ctx.render("t_slips.html", nav="students", slips=slips, klass=klass, code=code, token=token, cid=cid)


@page(T, "inst_admin")
def slips_pdf(ctx: Ctx) -> Response:
    try:
        data = _slips_signer(ctx).loads(str(ctx.form.get("token", "")), max_age=3600)
    except (BadSignature, SignatureExpired):
        raise ValidationError("These slips have expired. Reset the PINs to print new ones.") from None
    with ctx.tenant() as tx:
        require_class(tx, ctx.actor, data["c"])
        klass = tx.val("SELECT label FROM classes WHERE id=%s", [data["c"]])
        code = tx.val("SELECT school_code FROM institutions WHERE id=%s", [tx.inst])
    pdf = pdfs.login_slips(code, klass, data["s"])
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=login-slips-{klass}.pdf"})


@page(T, "inst_admin")
def student_action(ctx: Ctx) -> Response:
    eid, action = int(ctx.request.path_params["eid"]), ctx.request.path_params["action"]
    with ctx.tenant() as tx:
        row = tx.one("""SELECT e.class_id, e.roll_no, u.name FROM enrolments e JOIN users u ON u.id=e.student_id WHERE e.id=%s""", [eid])
        if not row:
            raise NotFound("That student was not found.")
        cid = row["class_id"]
        if action == "reset":
            pin = students.reset_pin(tx, ctx.actor, eid)
            result = [{"name": row["name"], "roll_no": row["roll_no"], "pin": pin}]
        elif action == "consent":
            students.record_consent(tx, ctx.actor, eid, parent_name=str(ctx.form.get("parent_name", "")),
                                    method=str(ctx.form.get("method", "")))
            return redirect(f"/teacher/classes/{cid}/students", f"Consent recorded for {row['name']}.")
        elif action == "deactivate":
            students.deactivate(tx, ctx.actor, eid)
            return redirect(f"/teacher/classes/{cid}/students", f"{row['name']} can no longer sign in. Their history is kept.")
        elif action == "move":
            students.move(tx, ctx.actor, eid, to_class_id=f_int(ctx.form.get("to_class_id"), "class"),
                          new_roll_no=str(ctx.form.get("roll_no", "")))
            return redirect(f"/teacher/classes/{cid}/students", f"{row['name']} moved.")
        else:
            raise NotFound("Unknown action.")
    return _slips(ctx, cid, result)


@page(*STAFF)
def tutor_log(ctx: Ctx) -> Response:
    sid = int(ctx.request.path_params["sid"])
    with ctx.tenant() as tx:
        log = learn.tutor_log(tx, ctx.actor, sid)
        student = tx.one("SELECT id, name FROM users WHERE id=%s", [sid])
        sessions = tx.all("""SELECT s.id, s.status, s.scores, s.started_at, t.title FROM teach_sessions s
                             JOIN syllabus_nodes t ON t.id=s.topic_id WHERE s.student_id=%s ORDER BY s.started_at DESC""", [sid])
    return ctx.render("t_tutorlog.html", nav="students", log=log, student=student, sessions=sessions)


@page(*STAFF)
def teach_transcript(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        view = teach.transcript(tx, ctx.actor, int(ctx.request.path_params["sid"]))
    return ctx.render("t_teach.html", nav="students", **view)


@page(*STAFF)
def student_report(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        pdf = reports.report_pdf(tx, ctx.actor, int(ctx.request.path_params["sid"]))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=progress-report.pdf"})


# ---------- mastery and bank ----------
@page(*STAFF)
def class_mastery(ctx: Ctx) -> Response:
    cid = int(ctx.request.path_params["cid"])
    fmt = ctx.request.query_params.get("format")
    with ctx.tenant() as tx:
        require_class(tx, ctx.actor, cid)
        label = tx.val("SELECT label FROM classes WHERE id=%s", [cid])
        data = dashboards.class_mastery(tx, cid)
        grid = dashboards.class_grid(tx, ctx.actor, cid, f_int(ctx.request.query_params.get("book"), "book", required=False),
                                     f_int(ctx.request.query_params.get("chapter"), "chapter", required=False))
    if fmt == "csv":
        return Response(dashboards.class_csv(data, label), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=mastery-{label}.csv"})
    if fmt == "pdf":
        rows = [[f"{r['code']} {r['title']}", r["mastered"], r["practised"], r["learning"], r["taught"],
                 f"{round(100 * float(r['avg_score']))}%" if r["avg_score"] is not None else ""] for r in data["rows"]]
        pdf = pdfs.table_report(f"Class {label}: mastery by topic", "Learning-progress indicators, not exam marks.",
                                ["Topic", "Mastered", "Practised", "Learning", "Taught NOVA", "Average"], rows)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=mastery-{label}.pdf"})
    return ctx.render("t_mastery.html", nav="progress", data=data, label=label, cid=cid, grid=grid)


@page(T)
def revise_selected(ctx: Ctx) -> Response:
    """From the grid: the ticked students and the chapter in view become a revision task on the topics they most
    need, set for just those students (the teacher picks the type and checks the questions as usual)."""
    cid, f = int(ctx.request.path_params["cid"]), ctx.form
    picked = [int(x) for x in f_list(f.get("students[]") or f.get("students"))]
    chapter = f_int(f.get("chapter"), "Chapter")
    book = f_int(f.get("book"), "Book")
    if not picked:
        return redirect(f"/teacher/classes/{cid}/mastery?book={book}&chapter={chapter}#grid", "Tick the students who need to revise first.")
    with ctx.tenant() as tx:
        in_class = {r["student_id"] for r in tx.all("SELECT student_id FROM enrolments WHERE class_id=%s AND status='active'", [cid])}
        picked = [s for s in picked if s in in_class]
        topics = dashboards.revision_topics(tx, ctx.actor, cid, chapter, picked)
    return redirect(f"/teacher/assignments/new?for={cid}:{book}&topics={','.join(map(str, topics))}"
                    f"&students={','.join(map(str, picked))}&kind=quiz",
                    f"Revision for {len(picked)} student{'s' if len(picked) != 1 else ''}: the topics they most need are ticked.")


@page(*STAFF)
def student_page(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = dashboards.student_profile(tx, ctx.actor, int(ctx.request.path_params["sid"]))
    return ctx.render("t_student.html", nav="students", **data)


@page(T)
def bank(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            ids = [int(q) for q in f_list(ctx.form.get("questions[]"))]
            aid = questions.create_from_bank(tx, ctx.actor, class_id=f_int(ctx.form.get("class_id"), "Class"), question_ids=ids,
                                             title=str(ctx.form.get("title", "")))
            return redirect(f"/teacher/assignments/{aid}", "Assignment made from your question bank. Schedule it below.")
        items = questions.bank(tx)
        classes = _classes(ctx, tx)
    return ctx.render("t_bank.html", nav="assignments", items=items, classes=classes)


@page(T)
def students_shortcut(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
    if not classes:
        raise NotFound("You aren't mapped to a class yet. Ask your school admin.")
    return redirect(f"/teacher/classes/{classes[0]['id']}/students")


@page(T, "inst_admin")
def classes_home(ctx: Ctx) -> Response:
    """Classes: one class goes straight to its page; several are listed with their size and progress."""
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
        if len(classes) == 1:
            return redirect(f"/teacher/classes/{classes[0]['id']}/students")
        for c in classes:
            c["students"] = tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [c["id"]])
            c["mastery"] = dashboards.class_mastery(tx, c["id"])["levels"]
    return ctx.render("t_classes.html", nav="classes", classes=classes)


@page(T)
def progress_shortcut(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        classes = _classes(ctx, tx)
    if not classes:
        raise NotFound("You aren't mapped to a class yet. Ask your school admin.")
    return redirect(f"/teacher/classes/{classes[0]['id']}/mastery")


# ---------- students' questions (1.31.0) ----------
@page(T)
def doubts_page(ctx: Ctx) -> Response:
    if ctx.request.method == "POST":
        with ctx.tenant() as tx:
            doubts.reply(tx, ctx.actor, f_int(ctx.form.get("doubt"), "Question"), str(ctx.form.get("reply", "")))
        return redirect("/teacher/doubts", "Reply sent. The student sees it in their bell.")
    with ctx.tenant() as tx:
        waiting = doubts.for_teacher(tx, ctx.actor, "open")
        answered = doubts.for_teacher(tx, ctx.actor, "answered")
    return ctx.render("t_doubts.html", nav="home", waiting=waiting, answered=answered)


# ---------- leadership ----------
@page("leader", "inst_admin")
def leader(ctx: Ctx) -> Response:
    q = ctx.request.query_params
    fmt = q.get("format")
    with ctx.tenant() as tx:
        data = principal.overview(tx, ctx.actor)
        inst = tx.one("SELECT name, kind FROM institutions WHERE id=%s", [tx.inst])
        school = principal.school(tx, ctx.actor, grade=q.get("grade") or None, subject=q.get("subject") or None)
    if fmt == "csv":
        return Response(dashboards.leadership_csv(data), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=classes.csv"})
    if fmt == "pdf":
        rows = [[c["label"], c["teachers"], c["students"], c["understanding"], c["active_pct"], c["on_time"]] for c in data["classes"]]
        pdf = pdfs.table_report(f"{inst['name']}: how classes are learning", "Learning-progress indicators, not exam marks.",
                                ["Class", "Teachers", "Students", "Understanding %", "Active this week %", "On time %"], rows)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=classes.pdf"})
    return ctx.render("l_home.html", nav="overview", data=data, inst=inst, school=school,
                      this_month=clock.to_ist(clock.now()).strftime("%Y-%m"))


@page("leader", "inst_admin")
def leader_class(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = principal.class_page(tx, ctx.actor, int(ctx.request.path_params["cid"]))
    return ctx.render("l_class.html", nav="overview", **data)


@page("leader")
def leader_nudge(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        name = principal.nudge_teacher(tx, ctx.actor, f_int(ctx.form.get("teacher_id"), "Teacher"), str(ctx.form.get("message", "")))
    return redirect("/leader#teachers", f"Reminder sent to {name}. It's in their bell on NovaQuest.")


@page("leader", "inst_admin")
def leader_report(ctx: Ctx) -> Response:
    month = principal.parse_month(ctx.request.query_params.get("month"))
    with ctx.tenant() as tx:
        data = principal.monthly(tx, ctx.actor, month)
    pdf = pdfs.monthly_report(data)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=novaquest-{month:%Y-%m}.pdf"})


routes = [
    Route("/teacher", home), Route("/teacher/assignments", assignment_list),
    Route("/teacher/assignments/new", new_assignment, methods=["GET", "POST"]),
    Route("/teacher/assignments/{aid:int}", assignment_detail),
    Route("/teacher/questions/{qid:int}/{action}", question_action, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/schedule", schedule, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/rewrite", rewrite_basic, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/own", add_own_question, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/approve-all", approve_all, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/remind", remind_missing, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/extend", extend, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/review", review_page),
    Route("/teacher/assignments/{aid:int}/worksheet", worksheet), Route("/teacher/assignments/{aid:int}/present", present),
    Route("/teacher/assignments/{aid:int}/release", release, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/students/{sid:int}", student_scorecard),
    Route("/teacher/responses/{rid:int}/override", override, methods=["POST"]),
    Route("/teacher/attempts/{att:int}/feedback", feedback, methods=["POST"]),
    Route("/teacher/attempts/{att:int}/suggest", suggest_note, methods=["POST"]),
    Route("/teacher/assignments/{aid:int}/message", class_message, methods=["POST"]),
    Route("/teacher/prep", unit_prep), Route("/teacher/present/{ch:int}", chapter_deck),
    Route("/teacher/present/{ch:int}/page/{tid:int}/{n:int}.jpg", deck_page_image),
    Route("/teacher/present/{ch:int}/fig/{tid:int}/{n:int}/{k:int}.jpg", deck_figure_image), Route("/teacher/prep/{ch:int}/{action}", unit_prep_action, methods=["POST"]),
    Route("/teacher/syllabus", syllabus_board), Route("/teacher/plan", term_plan, methods=["GET", "POST"]),
    Route("/teacher/timetable", timetable_page, methods=["GET", "POST"]),
    Route("/teacher/doubts", doubts_page, methods=["GET", "POST"]),
    Route("/teacher/syllabus/topics/{tid:int}/pictures", picture_preview),
    Route("/teacher/syllabus/topics/{tid:int}/pages", staff_book_pages),
    Route("/teacher/syllabus/topics/{tid:int}/pages/{n:int}.jpg", staff_book_page_image),
    Route("/teacher/topics/{tid:int}/open", topic_open, methods=["POST"]),
    Route("/teacher/syllabus/chapters", chapters_open, methods=["POST"]),
    Route("/teacher/topics/{tid:int}/notes", topic_notes, methods=["POST"]),
    Route("/teacher/classes/{cid:int}/students", class_students),
    Route("/teacher/classes/{cid:int}/students/add", add_student, methods=["POST"]),
    Route("/teacher/classes/{cid:int}/parent-reports", parent_reports, methods=["POST"]),
    Route("/teacher/classes/{cid:int}/students/import", import_students, methods=["POST"]),
    Route("/teacher/classes/{cid:int}/groups", make_group, methods=["POST"]),
    Route("/teacher/slips.pdf", slips_pdf, methods=["POST"]),
    Route("/teacher/enrolments/{eid:int}/{action}", student_action, methods=["POST"]),
    Route("/teacher/students/{sid:int}/tutor", tutor_log), Route("/teacher/students/{sid:int}/report.pdf", student_report),
    Route("/teacher/teach/{sid:int}", teach_transcript),
    Route("/teacher/classes/{cid:int}/mastery", class_mastery),
    Route("/teacher/assignments/{aid:int}/paper", paperwork_home, methods=["GET", "POST"]),
    Route("/teacher/paper/{sid:int}", paperwork_scan, methods=["GET", "POST"]),
    Route("/teacher/paper/{sid:int}/page/{n:int}", paperwork_photo),
    Route("/teacher/classes/{cid:int}/growth", class_growth), Route("/teacher/classes/{cid:int}/briefing", class_briefing),
    Route("/teacher/classes/{cid:int}/growth/new", new_growth_check, methods=["POST"]), Route("/teacher/classes/{cid:int}/escape", class_escape),
    Route("/teacher/escape/{ch:int}", escape_guide),
    Route("/teacher/classes/{cid:int}/revise", revise_selected, methods=["POST"]),
    Route("/teacher/students/{sid:int}", student_page),
    Route("/teacher/bank", bank, methods=["GET", "POST"]),
    Route("/teacher/papers", papers), Route("/teacher/papers/new", new_paper, methods=["GET", "POST"]),
    Route("/teacher/papers/{pid:int}", paper_page), Route("/teacher/papers/{pid:int}/{action}", paper_action, methods=["POST"]),
    Route("/teacher/papers/{pid:int}/set-{n:int}.pdf", paper_pdf),
    Route("/teacher/papers/{pid:int}/set-{n:int}-{kind}.pdf", paper_pdf),
    Route("/teacher/students", students_shortcut), Route("/teacher/progress", progress_shortcut),
    Route("/teacher/classes", classes_home),
    Route("/leader", leader), Route("/leader/classes/{cid:int}", leader_class),
    Route("/leader/nudge", leader_nudge, methods=["POST"]), Route("/leader/report.pdf", leader_report),
]
