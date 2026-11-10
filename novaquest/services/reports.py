"""Shareable parent progress reports (SX-6)."""
from __future__ import annotations

from datetime import timedelta

from novaquest.core import audit, clock, security
from novaquest.core.db import Db, Tx
from novaquest.core.errors import NotFound
from novaquest.services import dashboards, pdfs, review
from novaquest.services.access import Actor, require, require_class, student_class_id

LINK_DAYS = 14


def _overview_for(tx: Tx, student_id: int) -> tuple[dict, dict]:
    proxy = Actor(student_id, tx.institution_id, "student", ("student",), "")
    ov = dashboards.student_overview(tx, proxy)
    scores = []
    for a in ov["released"][:6]:
        try:
            sc = review.scorecard(tx, proxy, a["id"])
            scores.append({"title": a["title"], "right": sc["right"], "total": sc["total"],
                           "result_txt": f"{sc['right']} of {sc['total']} right", "note": sc["feedback"],
                           "teacher": sc["teacher_name"]})
        except NotFound:
            continue
    ov["released_scores"] = scores
    cid = student_class_id(tx, student_id)
    if cid is not None:  # 'Worth revising together': the same topics the teacher sees under Needs to improve
        needs = [t for n in dashboards.needs_improvement(tx, student_id, cid) for t in n["topics"]]
        if needs:
            ov["plan"] = [{"title": t["title"], "chapter": t["chapter"], "why": t["reasons"][0]}
                          for t in sorted(needs, key=lambda t: t["worst"])[:5]]
    student = tx.one("""SELECT u.name, c.label AS class_label FROM users u JOIN enrolments e ON e.student_id=u.id AND e.status='active'
                        JOIN classes c ON c.id=e.class_id WHERE u.id=%s""", [student_id])
    return student, ov


def report_pdf(tx: Tx, actor: Actor, student_id: int | None = None) -> bytes:
    """Download a one-page report: students for themselves, teachers for their students."""
    if actor.role == "student":
        student_id = actor.user_id
    else:
        cid = student_class_id(tx, student_id)
        if cid is None:
            raise NotFound("That student was not found.")
        require_class(tx, actor, cid)
    student, ov = _overview_for(tx, student_id)
    inst = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
    return pdfs.parent_report(student, ov, inst)


def create_share_link(tx: Tx, actor: Actor, base_url: str, student_id: int | None = None) -> str:
    """A private link a parent can open without an account, valid for two weeks."""
    if actor.role == "student":
        student_id = actor.user_id
    else:
        cid = student_class_id(tx, student_id)
        if cid is None:
            raise NotFound("That student was not found.")
        require_class(tx, actor, cid)
    token = security.new_token()
    tx.exec("INSERT INTO report_links(token_hash, institution_id, student_id, created_by, expires_at) VALUES (%s,%s,%s,%s,%s)",
            [security.token_hash(token), tx.inst, student_id, actor.user_id, clock.now() + timedelta(days=LINK_DAYS)])
    audit.record(tx, "report.share", actor_id=actor.user_id, target=f"user:{student_id}")
    return f"{base_url.rstrip('/')}/r/{token}"


def shared_view(db: Db, token: str) -> dict:
    """What a parent sees behind a share link (the student, their progress, the school, when the link ends), if valid."""
    with db.anonymous() as tx:
        row = tx.one("SELECT * FROM nq_report_link(%s)", [security.token_hash(token)])
    if not row or row["expires_at"] <= clock.now():
        raise NotFound("This report link has expired. Ask for a new one.")
    with db.tenant(row["institution_id"]) as tx:
        student, ov = _overview_for(tx, row["student_id"])
        inst = tx.val("SELECT name FROM institutions WHERE id=%s", [row["institution_id"]])
    return {"student": student, "ov": ov, "inst": inst, "expires_at": row["expires_at"]}


def shared_pdf(db: Db, token: str) -> bytes:
    """Render the report behind a share link as a PDF, if it is still valid."""
    v = shared_view(db, token)
    return pdfs.parent_report(v["student"], v["ov"], v["inst"])


def email_parents(tx: Tx, actor: Actor, class_id: int, base_url: str) -> dict[str, int]:
    """Email every parent in the class a private two-week link to their child's progress report (the same report as
    the PDF). Only students whose parental consent is recorded and who have a parent email; once a day per student.
    Returns how many were sent and why others were skipped."""
    from novaquest.services import notify

    require(actor, "teacher", "inst_admin")
    require_class(tx, actor, class_id)
    school = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
    rows = tx.all("""SELECT e.id, e.student_id, e.parent_email, e.consent_status, u.name FROM enrolments e
                     JOIN users u ON u.id=e.student_id WHERE e.class_id=%s AND e.status='active'""", [class_id])
    out = {"sent": 0, "no_email": 0, "no_consent": 0}
    day = clock.now().date().isoformat()
    for r in rows:
        if not r["parent_email"]:
            out["no_email"] += 1
            continue
        if r["consent_status"] != "recorded":
            out["no_consent"] += 1
            continue
        link = create_share_link(tx, actor, base_url, r["student_id"])
        first = r["name"].split()[0]
        body = (f"Dear parent,\n\nHere is {first}'s latest progress report from {school} on NovaQuest: the topics "
                f"{first} has learnt, practised and mastered, work handed in, and notes from teachers.\n\n{link}\n\n"
                f"The link is private to you and works for {LINK_DAYS} days. No account is needed.\n\n{actor.name}, {school}")
        if notify.queue_email(tx, r["parent_email"], f"{first}'s progress report", body, f"parent-report:{r['id']}:{day}"):
            tx.exec("UPDATE enrolments SET report_emailed_at=%s WHERE id=%s", [clock.now(), r["id"]])
            out["sent"] += 1
    audit.record(tx, "report.email_parents", actor_id=actor.user_id, target=f"class:{class_id}", detail=out)
    return out
