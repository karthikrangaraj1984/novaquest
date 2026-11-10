"""Institution onboarding: institutions, classes, staff accounts, teacher mapping, rollover (ON-1..ON-5)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from novaquest.core import audit, clock, security
from novaquest.core.db import Db, Tx
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import notify
from novaquest.services.access import Actor, require

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
STAFF_ROLE_NAMES = {"teacher": "teacher", "leader": "leader", "principal": "leader", "hod": "leader",
                    "admin": "inst_admin", "inst_admin": "inst_admin"}


@dataclass(frozen=True, slots=True)
class Invite:
    """A staff invitation (link is shown once and emailed)."""

    user_id: int
    email: str
    link: str


def create_institution(db: Db, actor: Actor, *, name: str, kind: str, board: str, academic_year: str,
                       school_code: str, admin_name: str, admin_email: str, base_url: str) -> tuple[int, Invite]:
    """NovaQuest admin creates an institution with its first admin (ON-1)."""
    require(actor, "platform_admin")
    code = school_code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{3,12}", code):
        raise ValidationError("School code must be 3–12 letters, numbers or dashes.", field="school_code")
    if kind not in ("school", "college"):
        raise ValidationError("Choose school or college.", field="kind")
    if not name.strip():
        raise ValidationError("Enter the institution's name.", field="name")
    with db.platform() as tx:
        if tx.val("SELECT 1 FROM institutions WHERE school_code=%s", [code]):
            raise Conflict(f"School code {code} is already used.")
        inst = tx.val("""INSERT INTO institutions(name, kind, board, academic_year, school_code) VALUES (%s,%s,%s,%s,%s)
                         RETURNING id""", [name.strip(), kind, board.strip(), academic_year.strip(), code])
        audit.record(tx, "institution.create", actor_id=actor.user_id, target=f"institution:{inst}",
                     detail={"name": name, "code": code}, institution_id=inst)
    with db.tenant(inst) as tx:
        invite = _invite_staff(tx, actor, admin_name, admin_email, ["inst_admin"], base_url)
    return inst, invite


def _invite_staff(tx: Tx, actor: Actor, name: str, email: str, roles: list[str], base_url: str) -> Invite:
    email = email.strip().lower()
    if not name.strip():
        raise ValidationError("Enter a name.", field="name")
    if not EMAIL_RE.match(email):
        raise ValidationError(f"“{email}” is not a valid email address.", field="email")
    if tx.val("SELECT 1 FROM nq_user_by_email(%s)", [email]):
        raise Conflict(f"{email} already has an account.")
    token = security.new_token()
    uid = tx.val("""INSERT INTO users(institution_id, name, email, status, invite_token_hash) VALUES (%s,%s,%s,'invited',%s)
                    RETURNING id""", [tx.inst, name.strip(), email, security.token_hash(token)])
    for role in roles:
        tx.exec("INSERT INTO user_roles(user_id, institution_id, role) VALUES (%s,%s,%s)", [uid, tx.inst, role])
    link = f"{base_url.rstrip('/')}/invite/{token}"
    notify.queue_email(tx, email, "You're invited to NovaQuest",
                       f"Hello {name.strip()},\n\nYou have been added to NovaQuest. Set your password here:\n{link}\n",
                       f"invite:{uid}")
    audit.record(tx, "staff.invite", actor_id=actor.user_id, target=f"user:{uid}", detail={"roles": roles})
    return Invite(uid, email, link)


def create_class(tx: Tx, actor: Actor, *, grade: str, section: str, academic_year: str) -> int:
    """Institution admin adds a grade + section (ON-2)."""
    require(actor, "inst_admin")
    grade, section = grade.strip(), section.strip().upper()
    if not grade or not section:
        raise ValidationError("Enter both grade and section.")
    label = f"{grade}{section}"
    if tx.val("SELECT 1 FROM classes WHERE label=%s AND academic_year=%s", [label, academic_year]):
        raise Conflict(f"Class {label} already exists for {academic_year}.")
    cid = tx.val("""INSERT INTO classes(institution_id, label, grade, section, academic_year) VALUES (%s,%s,%s,%s,%s)
                    RETURNING id""", [tx.inst, label, grade, section, academic_year])
    # Start with any ready-made (shared) books for this grade; the admin can change them (SY-6). Since 1.12.0 there
    # are none: the NCERT 2018 book is retired, and schools upload their own books.
    from novaquest.services import syllabus

    shared = [b["id"] for b in syllabus.available_books(tx, grade) if b["institution_id"] is None]
    if shared:
        syllabus.set_class_subjects(tx, cid, shared)
    audit.record(tx, "class.create", actor_id=actor.user_id, target=f"class:{cid}")
    return cid


def set_class_subjects(tx: Tx, actor: Actor, class_id: int, syllabus_ids: list[int]) -> None:
    """School admin chooses which books (subjects) a class studies (SY-6)."""
    require(actor, "inst_admin")
    from novaquest.services import syllabus

    syllabus.set_class_subjects(tx, class_id, syllabus_ids)
    audit.record(tx, "class.subjects", actor_id=actor.user_id, target=f"class:{class_id}", detail={"books": syllabus_ids})


def list_classes(tx: Tx) -> list[dict]:
    """Active classes with student counts."""
    return tx.all("""SELECT c.id, c.label, c.grade, c.section, c.academic_year,
        (SELECT count(*) FROM enrolments e WHERE e.class_id=c.id AND e.status='active') AS students
        FROM classes c WHERE c.status='active' ORDER BY c.grade, c.section""")


def invite_staff(tx: Tx, actor: Actor, *, name: str, email: str, roles: list[str], base_url: str) -> Invite:
    """Institution admin creates a teacher or leadership account by form (ON-3)."""
    require(actor, "inst_admin")
    roles = [r for r in roles if r in ("teacher", "leader", "inst_admin")]
    if not roles:
        raise ValidationError("Choose at least one role.", field="roles")
    return _invite_staff(tx, actor, name, email, roles, base_url)


def import_staff_csv(tx: Tx, actor: Actor, data: bytes, base_url: str) -> tuple[list[Invite], list[str]]:
    """Create staff from CSV with columns name,email,role (ON-3). Returns invites and row errors."""
    require(actor, "inst_admin")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace")))
    invites, errors = [], []
    for i, row in enumerate(reader, start=2):
        role = STAFF_ROLE_NAMES.get((row.get("role") or "teacher").strip().lower())
        if not role:
            errors.append(f"Row {i}: role “{row.get('role')}” should be teacher, principal, hod or admin.")
            continue
        try:
            with tx.conn.transaction():
                invites.append(_invite_staff(tx, actor, row.get("name", ""), row.get("email", ""), [role], base_url))
        except (ValidationError, Conflict) as exc:
            errors.append(f"Row {i}: {exc.message}")
    return invites, errors


def list_staff(tx: Tx) -> list[dict]:
    """Staff with roles and the classes they teach."""
    return tx.all("""SELECT u.id, u.name, u.email, u.status, array_agg(DISTINCT r.role) AS roles,
        coalesce((SELECT string_agg(DISTINCT c.label, ', ') FROM teaching_assignments ta JOIN classes c ON c.id=ta.class_id
                  WHERE ta.teacher_id=u.id), '') AS classes
        FROM users u JOIN user_roles r ON r.user_id=u.id WHERE r.role <> 'student'
        GROUP BY u.id ORDER BY u.name""")


def assign_teacher(tx: Tx, actor: Actor, *, teacher_id: int, class_id: int, syllabus_id: int) -> None:
    """Map a teacher to a subject and class (ON-4). For a book in parts the teacher gets every part."""
    require(actor, "inst_admin")
    from novaquest.services import syllabus

    if not tx.val("SELECT 1 FROM user_roles WHERE user_id=%s AND role='teacher'", [teacher_id]):
        raise NotFound("That teacher was not found.")
    if not tx.val("SELECT 1 FROM classes WHERE id=%s", [class_id]):
        raise NotFound("That class was not found.")
    available = {b["id"] for b in syllabus.available_books(tx)}
    if syllabus_id not in available and not tx.val("SELECT 1 FROM class_subjects WHERE class_id=%s AND syllabus_id=%s",
                                                   [class_id, syllabus_id]):
        raise NotFound("That book was not found.")
    for sid in [syllabus_id] + [i for i in syllabus.parts_of(tx, syllabus_id) if i != syllabus_id and i in available]:
        if not tx.val("SELECT 1 FROM class_subjects WHERE class_id=%s AND syllabus_id=%s", [class_id, sid]):
            tx.exec("""INSERT INTO class_subjects(institution_id, class_id, syllabus_id, ord)
                       VALUES (%s,%s,%s,(SELECT count(*) FROM class_subjects WHERE class_id=%s))""", [tx.inst, class_id, sid, class_id])
        tx.exec("""INSERT INTO teaching_assignments(institution_id, teacher_id, class_id, syllabus_id) VALUES (%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""", [tx.inst, teacher_id, class_id, sid])
    audit.record(tx, "teacher.assign", actor_id=actor.user_id, target=f"user:{teacher_id}", detail={"class_id": class_id})


def _grade_no(grade: str) -> int:
    digits = "".join(ch for ch in str(grade) if ch.isdigit())
    return int(digits) if digits else 0


def next_class(classes: list[dict], c: dict) -> dict | None:
    """The class a class moves up into: the next grade, same section (a later year if there is one)."""
    up = [x for x in classes if _grade_no(x["grade"]) == _grade_no(c["grade"]) + 1 and x["section"] == c["section"]]
    return max(up, key=lambda x: x["academic_year"], default=None)


def rollover(tx: Tx, actor: Actor, *, from_class_id: int, to_class_id: int) -> int:
    """Move every active student to the next class, keeping their history and logins (ON-5)."""
    require(actor, "inst_admin")
    if from_class_id == to_class_id:
        raise ValidationError("Choose a different class to move students into.")
    a = tx.one("SELECT label, grade, academic_year FROM classes WHERE id=%s AND status='active'", [from_class_id])
    b = tx.one("SELECT label, grade, academic_year FROM classes WHERE id=%s AND status='active'", [to_class_id])
    if not a or not b:
        raise ValidationError("Choose two active classes.")
    if _grade_no(b["grade"]) <= _grade_no(a["grade"]) and b["academic_year"] <= a["academic_year"]:
        raise ValidationError(f"{a['label']} → {b['label']} isn't moving up a year. To move one student between sections, "
                              "use Move on the teacher's class list.")
    rows = tx.all("""SELECT id, student_id, roll_no, pin_hash, parent_email, consent_status, consent_by, consent_method,
                     consent_at FROM enrolments WHERE class_id=%s AND status='active'""", [from_class_id])
    now = clock.now()
    for r in rows:
        tx.exec("UPDATE enrolments SET status='promoted', ended_at=%s WHERE id=%s", [now, r["id"]])
        tx.exec("""INSERT INTO enrolments(institution_id, class_id, student_id, roll_no, pin_hash, parent_email, consent_status,
                   consent_by, consent_method, consent_at, started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [tx.inst, to_class_id, r["student_id"], r["roll_no"], r["pin_hash"], r["parent_email"], r["consent_status"],
                 r["consent_by"], r["consent_method"], r["consent_at"], now])
    tx.exec("UPDATE classes SET status='archived' WHERE id=%s", [from_class_id])
    audit.record(tx, "class.rollover", actor_id=actor.user_id, target=f"class:{from_class_id}",
                 detail={"to": to_class_id, "students": len(rows)})
    return len(rows)
