"""Student pages: home, tasks, doing an assignment, scorecards, Learn mode, Teach NOVA and progress."""
from __future__ import annotations

import dataclasses

from starlette.responses import FileResponse, Response
from starlette.routing import Route

from novaquest import i18n
from novaquest.ai.factory import get_engine
from novaquest.core import clock
from novaquest.core.errors import ValidationError
from novaquest.services import (assignments, attempts, daily, dashboards, doubts, escape, examprep, language, learn, mastery, mistakes,
                                myprogress, pages, path, pictures, plans, reports, review, syllabus, teach)
from novaquest.web.core import Ctx, f_int, f_list, json_ok, page, redirect

S = "student"


def _engine(ctx: Ctx):
    return get_engine(ctx.settings, ctx.db, ctx.actor.institution_id)


def _learning(tx, actor, book_id: int | None = None) -> tuple[list[dict], dict | None, list[dict], dict | None]:
    """(chapters of the chosen book, where to continue across all books, the class's books, the chosen book)."""
    books = learn.student_books(tx, actor)
    book = syllabus.pick(books, book_id)
    every = learn.topics_for_student(tx, actor, all_books=True)
    path.annotate(tx, actor.user_id, every)
    chapters = [c for c in every if book and c["book_id"] == book["id"]]
    return chapters, path.continue_topic(tx, actor.user_id, every), books, book


def _book_param(ctx: Ctx) -> int | None:
    return f_int(ctx.request.query_params.get("book"), "book", required=False)


@page(S)
def home(ctx: Ctx) -> Response:
    """Today for a student (1.31.0): work closing in the next two days with its countdown, then the one learning step
    to do next (the topic being taught in class now first), Daily 5, new results with the change from last time,
    mistakes to redo and an exam coming up."""
    with ctx.tenant() as tx:
        items = assignments.for_student(tx, ctx.actor.user_id)
        chapters, cont, _, _ = _learning(tx, ctx.actor)
        in_class = _in_class(tx, ctx.actor, chapters)
        due = [a for a in items if a["view"] == "open"]
        soon = [a for a in due if (a["student_expires_at"] - clock.now()).total_seconds() < DUE_SOON_HOURS * 3600]
        later = [a for a in due if a not in soon]
        soon_label = "Due soon"
        if not soon and later:  # nothing in the next two days: still show the next deadline
            soon, later, soon_label = later[:1], later[1:], "Next due"
        info = {a["id"]: assignments.task_summary(tx, a) for a in soon[:HOME_WORK]}
        rows = myprogress.scores(tx, ctx.actor.user_id)
        fresh = myprogress.new_results(rows)[:3]
        d5 = daily.status(tx, ctx.actor)
        to_fix = mistakes.count(tx, ctx.actor.user_id)
        exam = next(iter(examprep.upcoming(tx, ctx.actor.user_id)), None)
        if exam and exam["days_left"] > 21:
            exam = None
    ic = next((x for x in in_class if x["topic"] and x["topic"].get("level", 0) < 3 and x["topic"]["path_next"]), None)
    up = ({"chapter": ic["chapter"], "topic": ic["topic"], "step": ic["topic"]["path_next"], "in_class": True,
           "resume": ic["topic"]["started"]} if ic else ({**cont, "in_class": False} if cont else None))
    return ctx.render("s_home.html", nav="home", soon=soon[:HOME_WORK], more_soon=max(0, len(soon) - HOME_WORK), later=later,
                      info=info, up=up, daily=d5, fresh=fresh, to_fix=to_fix, exam=exam, soon_label=soon_label)


DUE_SOON_HOURS = 48
HOME_WORK = 4  # open assignments listed on Home besides Up next


@page(S)
def daily_review(ctx: Ctx) -> Response:
    """Daily 5: today's five review questions, one at a time; POST marks them and shows what was right."""
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            row = daily.today_review(tx, ctx.actor)
            n = len(row["items"]) if row else 0
            res = daily.submit(tx, ctx.actor, {i: ctx.form.get(f"q{i}") for i in range(n)})
            return ctx.render("s_daily_result.html", nav="home", res=res)
        row = daily.today_review(tx, ctx.actor)
        st = daily.status(tx, ctx.actor)
    if row and row["done_at"]:
        return redirect("/student", "You've done today's Daily 5. A new one comes tomorrow.")
    qs = [{"i": i, "kind": "mcq", "prompt": q["prompt"], "options": q["options"], "scenario": None, "topic": q["topic"]}
          for i, q in enumerate(row["items"])] if row else []
    return ctx.render("s_daily.html", nav="home", questions=qs, streak=(st or {}).get("streak", 0))


@page(S)
def tasks(ctx: Ctx) -> Response:
    """Every task, all subjects together by deadline, with a filter per subject; open work split into In progress and
    Not started."""
    with ctx.tenant() as tx:
        items = assignments.for_student(tx, ctx.actor.user_id)
        scores = {r["id"]: r for r in myprogress.with_change(myprogress.scores(tx, ctx.actor.user_id))}
    subjects = list({a["subject_id"]: {"id": a["subject_id"], "label": a["subject_label"], "colour": a["subject_colour"],
                                        "open": 0} for a in items}.values())
    for s in subjects:
        s["open"] = sum(1 for a in items if a["subject_id"] == s["id"] and a["view"] == "open")
    pick = _book_param(ctx) if ctx.request.query_params.get("book") else None
    if pick is not None:  # any part's id picks its whole subject
        pick = next((a["subject_id"] for a in items if pick in (a["syllabus_id"], a["subject_id"])), pick)
    shown = [a for a in items if pick is None or a["subject_id"] == pick]
    return ctx.render("s_tasks.html", nav="tasks", items=shown, subjects=subjects if len(subjects) > 1 else [], pick=pick,
                      scores=scores)


@page(S)
def open_assignment(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        a = assignments.get(tx, aid)
        att = attempts.start_or_resume(tx, ctx.actor, aid)
        if a["kind"] in ("learn", "teach"):
            first = a["topic_ids"][0]
            return redirect(f"/student/teach/{first}" if a["kind"] == "teach" else f"/student/learn/{first}")
        if a["kind"] == "escape":  # an escape room set as work: play the room; it's handed in when they escape
            q = tx.one("""SELECT q.answer FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                          WHERE aq.assignment_id=%s AND q.kind='escape' LIMIT 1""", [aid])
            ch, bk = int(q["answer"]["chapter"]), int(q["answer"]["syllabus_id"])
            finished = escape.progress_assignments(tx, ctx.actor, bk, ch)  # already escaped before it was set
    if a["kind"] == "escape":
        for attempt_id in finished:
            attempts.submit(ctx.db, _engine(ctx), ctx.actor, attempt_id)
        return redirect(f"/student/escape/{ch}?book={bk}", "You've already escaped this room: handed in." if finished else None)
    return redirect(f"/student/attempts/{att['id']}")


@page(S)
def attempt_page(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = attempts.view(tx, ctx.actor, int(ctx.request.path_params["att"]))
    if data["attempt"]["status"] != "in_progress":
        return redirect("/student/tasks", "Handed in. Your teacher will release results soon.")
    return ctx.render("s_attempt.html", nav="tasks", claude=getattr(_engine(ctx), "name", "") == "claude", **data)


@page(S, api=True)
def api_save(ctx: Ctx) -> Response:
    body = ctx.json or {}
    with ctx.tenant() as tx:
        saved = attempts.save_answer(tx, ctx.actor, int(ctx.request.path_params["att"]),
                                     f_int(body.get("question_id"), "question_id"), body.get("answer") or {})
    return json_ok(saved_at=saved)


@page(S, api=True)
def api_hint(ctx: Ctx) -> Response:
    reply = attempts.hint(ctx.db, _engine(ctx), ctx.actor, int(ctx.request.path_params["att"]),
                          int(ctx.request.path_params["qid"]), str((ctx.json or {}).get("text", "")))
    return json_ok(text=reply.text, citations=list(reply.citations))


@page(S, api=True)
def api_translate_question(ctx: Ctx) -> Response:
    """A question in the student's language (1.22.0)."""
    out = attempts.translate_question(ctx.db, _engine(ctx), ctx.actor, int(ctx.request.path_params["att"]),
                                      int(ctx.request.path_params["qid"]))
    return json_ok(**out)


@page(S)
def set_language(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        language.set_lang(tx, ctx.actor, str(ctx.form.get("lang", "")))
    return redirect("/student/progress#lang-h", i18n.translator(str(ctx.form.get("lang", "en")))("Save") + " ✓")


def _nova_in_my_language(ctx: Ctx, view: dict) -> dict:
    """Teach NOVA's past messages in the student's language (kept translations; English if none)."""
    with ctx.tenant() as tx:
        lang = language.student_lang(tx, ctx.actor.user_id)
    nova = [t for t in view.get("turns", []) if t["role"] == "nova"]
    if lang != "en" and nova:
        for t, text in zip(nova, language.localise(ctx.db, _engine(ctx), ctx.actor.institution_id, [t["body"] for t in nova], lang)):
            t["body"] = text
    return view


@page(S)
def submit(ctx: Ctx) -> Response:
    attempts.submit(ctx.db, _engine(ctx), ctx.actor, int(ctx.request.path_params["att"]))
    return redirect("/student/tasks?yay=task")


@page(S)
def results(ctx: Ctx) -> Response:
    aid = int(ctx.request.path_params["aid"])
    with ctx.tenant() as tx:
        card = review.scorecard(tx, ctx.actor, aid)
        levels = mastery.for_student(tx, ctx.actor.user_id)
        felt = myprogress.reflection(tx, ctx.actor.user_id, aid)
        to_fix = mistakes.count(tx, ctx.actor.user_id)
    topics = [{"name": c["name"], "topic_id": c["topic_id"], "level": levels.get(c["topic_id"], {}).get("level", 0)}
              for c in card["concepts"] if not c.get("ai") and c.get("topic_id")]
    return ctx.render("s_scorecard.html", nav="tasks", card=card, topics=topics, tab=ctx.request.query_params.get("tab", "summary"),
                      felt=felt, feelings=myprogress.FEELINGS, next_steps=myprogress.NEXT_STEPS, to_fix=to_fix)


@page(S)
def reflect(ctx: Ctx) -> Response:
    """How the work felt and what the student will do next; the chosen next step opens straight away."""
    aid = int(ctx.request.path_params["aid"])
    step = str(ctx.form.get("next_step", ""))
    with ctx.tenant() as tx:
        myprogress.reflect(tx, ctx.actor, aid, str(ctx.form.get("feeling", "")), step)
    go = {"mistakes": "/student/mistakes", "ask": f"/student/questions?assignment={aid}#ask"}.get(step)
    return redirect(go or f"/student/results/{aid}#reflect-h", "Thanks! Your teacher sees how the class found it (not your name with it).")


def _topic_cheer(tx, actor, topic: dict, steps: dict) -> tuple[str, str] | None:
    """A celebration when every step of a topic is done, with the weekly streak to keep going."""
    if not steps["complete"]:
        return None
    weeks = dashboards.week_streak(tx, actor.user_id)
    more = f" You've learnt {weeks} weeks in a row." if weeks > 1 else ""
    return ("Topic complete! 🎉", f"You finished all {steps['total']} steps of {topic['title']}.{more}")


# ---------- escape rooms: learn AI through AI (LN-12) ----------
@page(S)
def escape_home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        rooms = escape.student_rooms(tx, ctx.actor)
        skills = escape.student_skills(tx, ctx.actor.user_id)
    return ctx.render("s_escape_index.html", nav="learn", rooms=rooms, skills=skills, hide_ask=True)


def _escape_answer(lock: dict, form) -> dict:
    """The answer to a lock, from its form."""
    kind = lock["kind"]
    if kind == "sort":
        return {"features": [str(x) for x in f_list(form.get("features[]"))]}
    if kind == "clean":
        return {"flag": [int(x) for x in f_list(form.get("flag[]")) if str(x).isdigit()]}
    if kind == "evidence":
        v, e = form.get("verdict"), form.get("evidence")
        return {"verdict": int(v) if str(v).isdigit() else None, "evidence": int(e) if str(e).isdigit() else None}
    c = form.get("choice")
    return {"choice": int(c) if str(c).isdigit() else None}


@page(S)
def escape_room(ctx: Ctx) -> Response:
    """Play a room. POST tries the current lock, asks for its hint, or types the door code."""
    ch = int(ctx.request.path_params["ch"])
    bk = _book_param(ctx)
    result, hint_text, error, door_wrong, escaped_now = None, None, None, False, False
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            action = str(ctx.form.get("action", "try"))
            v = escape.view(tx, ctx.actor, ch, bk)
            bk = v["book"]["id"]
            try:
                if action == "door":
                    escaped_now = escape.open_door(tx, ctx.actor, ch, str(ctx.form.get("code", "")), bk)
                    door_wrong = not escaped_now
                elif v["current"]:
                    n = v["current"]["no"]
                    if action == "hint":
                        hint_text = escape.hint(tx, ctx.actor, ch, n, bk)
                    else:
                        result = escape.attempt(tx, ctx.actor, ch, n, _escape_answer(v["current"], ctx.form), bk)
                        result["lock_no"] = n
            except ValidationError as exc:
                error = exc.message
        v = escape.view(tx, ctx.actor, ch, bk)
        finished = escape.progress_assignments(tx, ctx.actor, v["book"]["id"], ch) if ctx.request.method == "POST" else []
        if hint_text is None and v["current"] and v["current"]["hints"]:
            hint_text = v["current"]["hint"]  # a hint already shown stays visible
        skills = escape.student_skills(tx, ctx.actor.user_id) if v["escaped"] else None
    for attempt_id in finished:  # escape-room work is handed in the moment they escape
        attempts.submit(ctx.db, _engine(ctx), ctx.actor, attempt_id)
    if escaped_now:
        return redirect(f"/student/escape/{ch}?book={bk}&yay=escape")
    return ctx.render("s_escape_room.html", nav="learn", hide_ask=True, v=v, result=result, hint_text=hint_text,
                      error=error, door_wrong=door_wrong, skills=skills, status=422 if error else 200)


# ---------- Learn mode ----------
@page(S)
def learn_home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        chapters, cont, books, book = _learning(tx, ctx.actor, _book_param(ctx))
        in_class = _in_class(tx, ctx.actor, chapters)
        rooms = {f"{r['book_id']}:{r['chapter']}": r for r in escape.student_rooms(tx, ctx.actor) if book and r["book_id"] in book["ids"]}
    return ctx.render("s_learn.html", nav="learn", chapters=chapters, cont=cont, books=books, book=book, in_class=in_class,
                      rooms=rooms)


def _in_class(tx, actor, chapters: list[dict]) -> list[dict]:
    """Chapters of the shown book being taught in class now (term plan), each with the first topic not yet done."""
    from novaquest.services.access import student_class_id

    class_id = student_class_id(tx, actor.user_id)
    if class_id is None:
        return []
    now = {p["chapter_id"]: p for p in plans.in_class_now(tx, class_id)}
    out = []
    for ch in chapters:
        if ch["id"] in now:
            first = next((t for t in ch["topics"] if t["path_done"] < t["path_total"]), ch["topics"][0] if ch["topics"] else None)
            out.append({"chapter": ch, "ends_on": now[ch["id"]]["ends_on"], "topic": first})
    return out


@page(S)
def lesson(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        data = learn.lesson(tx, ctx.actor, tid)
        level = mastery.for_student(tx, ctx.actor.user_id).get(tid, {}).get("level", 0)
        steps = path.topic_path(tx, ctx.actor.user_id, data["topic"])
        book_pages = pages.page_view(tx, tid)
    return ctx.render("s_lesson.html", nav="learn", inline_ask=True, level=level, path=steps, book_pages=book_pages,
                      next_step=path.next_for(level, steps, tid), **data)


# ---------- book pages (LN-11) ----------
@page(S)
def book_pages(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        learn._require_open(tx, ctx.actor, tid)
        view = pages.page_view(tx, tid)
    return ctx.render("pages.html", nav="learn", base=f"/student/learn/{tid}", layout="student.html", **view)


@page(S)
def book_page_image(ctx: Ctx) -> Response:
    tid, n = int(ctx.request.path_params["tid"]), int(ctx.request.path_params["n"])
    with ctx.tenant() as tx:
        learn._require_open(tx, ctx.actor, tid)
        img = pages.render(tx, tid, n, ctx.settings.data_dir)
    return FileResponse(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- Learn in pictures ----------
@page(S)
def pictures_home(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        books = learn.student_books(tx, ctx.actor)
        book = syllabus.pick(books, _book_param(ctx))
        chapters = pictures.index(tx, ctx.actor, book["id"] if book else None)
    return ctx.render("s_pictures_index.html", nav="learn", chapters=chapters, books=books, book=book)


@page(S)
def pictures_deck(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        data = pictures.deck(tx, ctx.actor, int(ctx.request.path_params["tid"]))
        data["path"] = path.topic_path(tx, ctx.actor.user_id, data["topic"])
    return ctx.render("s_pictures.html", nav="learn", **data)


@page(S, api=True)
def api_pictures_check(ctx: Ctx) -> Response:
    body = ctx.json or {}
    with ctx.tenant() as tx:
        res = pictures.check(tx, ctx.actor, int(ctx.request.path_params["tid"]), f_int(body.get("index"), "index"),
                             f_int(body.get("choice"), "choice"))
    return json_ok(**res)


@page(S, api=True)
def api_pictures_done(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        level = pictures.finish(tx, ctx.actor, int(ctx.request.path_params["tid"]))
    return json_ok(level=level)


@page(S, api=True)
def api_check(ctx: Ctx) -> Response:
    body = ctx.json or {}
    with ctx.tenant() as tx:
        res = learn.answer_check(tx, ctx.actor, int(ctx.request.path_params["tid"]), f_int(body.get("index"), "index"),
                                 f_int(body.get("choice"), "choice"))
    return json_ok(**res)


@page(S, api=True)
def api_ask(ctx: Ctx) -> Response:
    body = ctx.json or {}
    reply = learn.ask(ctx.db, _engine(ctx), ctx.actor, str(body.get("question", "")), f_int(body.get("topic_id"), "topic", required=False))
    return json_ok(text=reply.text, citations=list(reply.citations), declined=reply.declined)


@page(S)
def practice(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            answers = {i: ctx.form.get(f"q{i}") for i in range(10)}
            res = learn.practice_submit(tx, ctx.actor, tid, answers)
            topic = syllabus.get_topic(tx, tid)
            steps = path.topic_path(tx, ctx.actor.user_id, topic)
            return ctx.render("s_practice_result.html", nav="learn", res=res, topic=topic, path=steps,
                              celebrate=_topic_cheer(tx, ctx.actor, topic, steps))
        data = learn.practice(tx, ctx.actor, tid)
    return ctx.render("s_practice.html", nav="learn", **data)


@page(S)
def understand(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    if ctx.request.method == "POST":
        res = learn.understanding_check(ctx.db, _engine(ctx), ctx.actor, tid, str(ctx.form.get("explain", "")),
                                        str(ctx.form.get("apply", "")))
        with ctx.tenant() as tx:
            topic = syllabus.get_topic(tx, tid)
            steps = path.topic_path(tx, ctx.actor.user_id, topic)
            cheer = _topic_cheer(tx, ctx.actor, topic, steps)
        return ctx.render("s_understand_result.html", nav="learn", res=res, topic=topic, path=steps, celebrate=cheer)
    with ctx.tenant() as tx:
        topic = syllabus.get_topic(tx, tid)
        prompt = learn.understanding_prompt(tx, ctx.actor, tid)
    parts = prompt.split(" Part 2 (apply): ")
    return ctx.render("s_understand.html", nav="learn", hide_ask=True, topic=topic, explain_q=parts[0].replace("Part 1 (explain): ", ""),
                      apply_q=parts[1] if len(parts) > 1 else "Give an example from daily life.")


# ---------- Teach NOVA ----------
@page(S)
def teach_page(ctx: Ctx) -> Response:
    tid = int(ctx.request.path_params["tid"])
    with ctx.tenant() as tx:
        att_id = tx.val("""SELECT t.id FROM attempts t JOIN assignments a ON a.id=t.assignment_id WHERE t.student_id=%s
                           AND t.status='in_progress' AND a.kind='teach' AND %s = ANY(a.topic_ids) LIMIT 1""", [ctx.actor.user_id, tid])
        if ctx.request.query_params.get("new") == "1":
            tx.exec("UPDATE teach_sessions SET status='ended', ended_at=now() WHERE student_id=%s AND topic_id=%s AND status='active'",
                    [ctx.actor.user_id, tid])
        view = teach.start(tx, ctx.actor, tid, att_id)
    return ctx.render("s_teach.html", nav="learn", hide_ask=True, **_nova_in_my_language(ctx, view))


@page(S)
def teach_done(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        view = teach.session_view(tx, ctx.actor, int(ctx.request.path_params["sid"]))
        cheer = None
        if view["session"]["status"] == "passed":
            topic = syllabus.get_topic(tx, view["topic"]["id"])
            cheer = _topic_cheer(tx, ctx.actor, topic, path.topic_path(tx, ctx.actor.user_id, topic))
    return ctx.render("s_teach.html", nav="learn", hide_ask=True, celebrate=cheer, **_nova_in_my_language(ctx, view))


@page(S, api=True)
def api_teach(ctx: Ctx) -> Response:
    res = teach.turn(ctx.db, _engine(ctx), ctx.actor, int(ctx.request.path_params["sid"]), str((ctx.json or {}).get("message", "")))
    return json_ok(reply=res["reply"], verdict=res["verdict"], ended=res["ended"], passed=res.get("passed", False))


# ---------- progress ----------
@page(S)
def progress(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        view = _my_progress(tx, ctx.actor)
    return ctx.render("s_progress.html", nav="progress", share=ctx.request.query_params.get("share"), **view)


def _my_progress(tx, actor) -> dict:
    """Everything on My progress (1.31.0): score trend and summary per subject, best and most improved, strong topics
    and the ones to work on, the weekly goal and streaks, exams, mistakes and questions to the teacher; then chapters,
    AI skills and Teach NOVA."""
    ov = dashboards.student_overview(tx, actor)
    rows = myprogress.scores(tx, actor.user_id)
    return {"ov": ov, "subjects": myprogress.subjects(rows), "results": list(reversed(myprogress.with_change(rows)))[:6],
            "n_results": len(rows), "best": myprogress.best(rows), "tp": myprogress.topics(tx, actor.user_id),
            "goal": myprogress.goal(tx, actor.user_id), "daily_streak": daily.streak(tx, actor.user_id),
            "exams": examprep.upcoming(tx, actor.user_id), "to_fix": mistakes.count(tx, actor.user_id),
            "asked": doubts.mine(tx, actor.user_id)[:3],
            "skills": escape.student_skills(tx, actor.user_id), "rooms": escape.student_rooms(tx, actor)}


@page(S)
def set_goal(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        myprogress.set_goal(tx, ctx.actor, f_int(ctx.form.get("target"), "Goal"))
    return redirect("/student/progress#goal-h", "Goal set for this week. You can do it!")


@page(S)
def report_pdf(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        pdf = reports.report_pdf(tx, ctx.actor)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=my-progress.pdf"})


@page(S)
def report_share(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        link = reports.create_share_link(tx, ctx.actor, ctx.settings.base_url)
    with ctx.tenant() as tx:
        view = _my_progress(tx, ctx.actor)
    return ctx.render("s_progress.html", nav="progress", share=link, **view)


# ---------- my mistakes, exam mode, ask my teacher (1.31.0) ----------
@page(S)
def my_mistakes(ctx: Ctx) -> Response:
    """Mistakes to fix: a redo round of up to five multiple-choice questions (POST marks it), and written answers to
    read again."""
    topic = f_int(ctx.request.query_params.get("topic"), "topic", required=False)
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            ids = [int(x) for x in f_list(ctx.form.get("qid[]")) if str(x).isdigit()]
            orders = {q: [int(i) for i in str(ctx.form.get(f"order_{q}", "")).split(",") if i.isdigit()] for q in ids}
            res = mistakes.submit_round(tx, ctx.actor, {q: ctx.form.get(f"a_{q}") for q in ids}, orders)
            return ctx.render("s_mistakes_result.html", nav="progress", res=res)
        ov = mistakes.overview(tx, ctx.actor.user_id)
        round_ = mistakes.redo_round(tx, ctx.actor.user_id, topic)
    return ctx.render("s_mistakes.html", nav="progress", ov=ov, round=round_, topic=topic)


@page(S)
def mistake_understood(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        mistakes.understood(tx, ctx.actor, int(ctx.request.path_params["qid"]))
    return redirect("/student/mistakes#written", "Marked as understood ✓")


@page(S)
def exams_home(ctx: Ctx) -> Response:
    f = ctx.form if ctx.request.method == "POST" else {}
    error = None
    with ctx.tenant() as tx:
        if f:
            try:
                pid = examprep.create(tx, ctx.actor, title=str(f.get("title", "")), syllabus_id=f_int(f.get("book"), "Subject"),
                                      chapter_ids=[int(x) for x in f_list(f.get("chapters[]")) if str(x).isdigit()],
                                      exam_on=plans.parse_day(str(f.get("exam_on", "")), "Exam date"))
                return redirect(f"/student/exams/{pid}", "Your revision plan is ready. A little every day!")
            except ValidationError as exc:
                error = exc.message
        mine = examprep.upcoming(tx, ctx.actor.user_id)
        choices = examprep.choices(tx, ctx.actor.user_id)
    return ctx.render("s_exams.html", nav="progress", exams=mine, choices=choices, f=f, error=error,
                      today=myprogress.today(), status=422 if error else 200)


@page(S)
def exam_page(ctx: Ctx) -> Response:
    with ctx.tenant() as tx:
        v = examprep.view(tx, ctx.actor, int(ctx.request.path_params["pid"]))
    return ctx.render("s_exam.html", nav="progress", v=v)


@page(S)
def exam_action(ctx: Ctx) -> Response:
    pid, action = int(ctx.request.path_params["pid"]), ctx.request.path_params["action"]
    with ctx.tenant() as tx:
        if action == "paper":
            xid = examprep.start_paper(tx, ctx.actor, pid)
            return redirect(f"/student/papers/{xid}")
        if action == "delete":
            examprep.remove(tx, ctx.actor, pid)
            return redirect("/student/exams", "Exam removed.")
    raise ValidationError("Unknown action.")


@page(S)
def practice_paper(ctx: Ctx) -> Response:
    xid = int(ctx.request.path_params["xid"])
    with ctx.tenant() as tx:
        if ctx.request.method == "POST":
            row = examprep.paper(tx, ctx.actor, xid)
            res = examprep.submit_paper(tx, ctx.actor, xid, {i: ctx.form.get(f"q{i}") for i in range(len(row["items"]))})
            return ctx.render("s_paper_result.html", nav="progress", res=res, title=row["title"])
        row = examprep.paper(tx, ctx.actor, xid)
    if row["done_at"]:
        return redirect(f"/student/exams/{row['plan_id']}", "You've handed in that practice paper.")
    left = max(0, int((row["ends_at"] - clock.now()).total_seconds()))
    return ctx.render("s_paper.html", nav="progress", row=row, seconds_left=left)


@page(S)
def my_questions(ctx: Ctx) -> Response:
    """Ask my teacher: write a question (about a topic or a question from my work) and see the replies."""
    q = ctx.request.query_params
    f = ctx.form if ctx.request.method == "POST" else {}
    error = None
    with ctx.tenant() as tx:
        if f:
            try:
                doubts.ask(tx, ctx.actor, str(f.get("body", "")), topic_id=f_int(f.get("topic"), "topic", required=False),
                           question_id=f_int(f.get("q"), "question", required=False),
                           assignment_id=f_int(f.get("assignment"), "assignment", required=False))
                return redirect("/student/questions", "Sent to your teacher. You'll see the reply here and in your bell.")
            except ValidationError as exc:
                error = exc.message
        about = None
        topic = f_int(q.get("topic") or f.get("topic"), "topic", required=False)
        qid = f_int(q.get("q") or f.get("q"), "question", required=False)
        if qid:
            about = tx.one("SELECT id, prompt FROM questions WHERE id=%s", [qid])
        elif topic:
            about = tx.one("SELECT id, title AS prompt FROM syllabus_nodes WHERE id=%s", [topic])
        mine = doubts.mine(tx, ctx.actor.user_id)
    return ctx.render("s_questions.html", nav="progress", mine=mine, about=about, topic=topic, qid=qid,
                      assignment=q.get("assignment") or f.get("assignment") or "", f=f, error=error, status=422 if error else 200)


routes = [
    Route("/student", home), Route("/student/tasks", tasks),
    Route("/student/assignments/{aid:int}", open_assignment), Route("/student/attempts/{att:int}", attempt_page),
    Route("/api/attempts/{att:int}/answers", api_save, methods=["POST"]),
    Route("/api/attempts/{att:int}/questions/{qid:int}/hint", api_hint, methods=["POST"]),
    Route("/api/attempts/{att:int}/questions/{qid:int}/translate", api_translate_question, methods=["POST"]),
    Route("/student/language", set_language, methods=["POST"]),
    Route("/student/attempts/{att:int}/submit", submit, methods=["POST"]),
    Route("/student/results/{aid:int}", results),
    Route("/student/daily", daily_review, methods=["GET", "POST"]),
    Route("/student/learn", learn_home), Route("/student/learn/{tid:int}", lesson),
    Route("/student/learn/{tid:int}/pages", book_pages), Route("/student/learn/{tid:int}/pages/{n:int}.jpg", book_page_image),
    Route("/student/escape", escape_home), Route("/student/escape/{ch:int}", escape_room, methods=["GET", "POST"]),
    Route("/student/pictures", pictures_home), Route("/student/learn/{tid:int}/pictures", pictures_deck),
    Route("/api/learn/{tid:int}/pictures/check", api_pictures_check, methods=["POST"]),
    Route("/api/learn/{tid:int}/pictures/done", api_pictures_done, methods=["POST"]),
    Route("/api/learn/{tid:int}/check", api_check, methods=["POST"]), Route("/api/learn/ask", api_ask, methods=["POST"]),
    Route("/student/learn/{tid:int}/practice", practice, methods=["GET", "POST"]),
    Route("/student/learn/{tid:int}/understand", understand, methods=["GET", "POST"]),
    Route("/student/teach/{tid:int}", teach_page), Route("/student/teach/session/{sid:int}", teach_done),
    Route("/api/teach/{sid:int}/turn", api_teach, methods=["POST"]),
    Route("/student/progress", progress), Route("/student/report.pdf", report_pdf),
    Route("/student/report/share", report_share, methods=["POST"]),
    Route("/student/goal", set_goal, methods=["POST"]),
    Route("/student/results/{aid:int}/reflect", reflect, methods=["POST"]),
    Route("/student/mistakes", my_mistakes, methods=["GET", "POST"]),
    Route("/student/mistakes/{qid:int}/understood", mistake_understood, methods=["POST"]),
    Route("/student/exams", exams_home, methods=["GET", "POST"]), Route("/student/exams/{pid:int}", exam_page),
    Route("/student/exams/{pid:int}/{action}", exam_action, methods=["POST"]),
    Route("/student/papers/{xid:int}", practice_paper, methods=["GET", "POST"]),
    Route("/student/questions", my_questions, methods=["GET", "POST"]),
]
