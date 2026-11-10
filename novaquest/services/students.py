"""Student onboarding by the class teacher: add, CSV import, login slips, PIN reset, consent,
deactivate and move (ST-1..ST-5)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from novaquest.core import audit, clock, security
from novaquest.core.db import Tx
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services.access import Actor, require, require_class

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_COMMON_TYPOS = {"gmial": "gmail", "gamil": "gmail", "gmal": "gmail", "yahooo": "yahoo", "hotmial": "hotmail"}
MAX_ROWS = 200


@dataclass(slots=True)
class RowCheck:
    """One CSV row after validation."""

    row: int
    roll_no: str
    name: str
    parent_email: str
    status: str  # ready | fix | duplicate
    message: str = ""


@dataclass(slots=True)
class ImportResult:
    """Outcome of checking (and optionally adding) a class list."""

    rows: list[RowCheck] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)

    @property
    def ready(self) -> int:
        return sum(r.status == "ready" for r in self.rows)

    @property
    def to_fix(self) -> int:
        return sum(r.status == "fix" for r in self.rows)


def _email_problem(email: str) -> str | None:
    if not email:
        return None
    if not EMAIL_RE.match(email):
        domain = email.split("@")[-1].split(".")[0].lower() if "@" in email else ""
        if domain in _COMMON_TYPOS:
            return f"This email looks incomplete. Did you mean {_COMMON_TYPOS[domain]}.com?"
        return "This email looks incomplete. Check it or leave it blank."
    return None


def _taken_rolls(tx: Tx, class_id: int) -> dict[str, str]:
    return {r["roll_no"]: r["name"] for r in tx.all("""SELECT e.roll_no, u.name FROM enrolments e JOIN users u ON u.id=e.student_id
                                                       WHERE e.class_id=%s AND e.status='active'""", [class_id])}


def parse_rows(data: bytes, filename: str = "list.csv") -> list[dict[str, str]]:
    """Read a CSV or Excel class list into rows with roll_no, name, parent_email."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl  # lazy: only for Excel uploads

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        it = wb.active.iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it, [])]
        raw = [dict(zip(header, ["" if v is None else str(v) for v in row])) for row in it]
    else:
        raw = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace"))))
    out = []
    for r in raw:
        norm = {k.strip().lower().replace(" ", "_").replace(".", ""): (v or "").strip() for k, v in r.items() if k}
        roll = norm.get("roll_no") or norm.get("roll") or norm.get("roll_number") or ""
        name = norm.get("student_name") or norm.get("name") or ""
        email = norm.get("parent_email") or norm.get("email") or ""
        if roll or name or email:
            out.append({"roll_no": roll.removesuffix(".0"), "name": name, "parent_email": email})
    if len(out) > MAX_ROWS:
        raise ValidationError(f"A class list can have at most {MAX_ROWS} students.")
    return out


def check_rows(tx: Tx, class_id: int, rows: list[dict[str, str]]) -> ImportResult:
    """Validate every row and explain problems in plain words, without saving anything."""
    taken = _taken_rolls(tx, class_id)
    seen: dict[str, int] = {}
    result = ImportResult()
    for i, r in enumerate(rows, start=2):
        roll, name, email = r["roll_no"].strip(), " ".join(r["name"].split()), r["parent_email"].strip()
        check = RowCheck(i, roll, name, email, "ready")
        if not name:
            check.status, check.message = "fix", "Student name is missing."
        elif not re.fullmatch(r"[0-9A-Za-z-]{1,10}", roll or ""):
            check.status, check.message = "fix", "Roll number is missing or has spaces."
        elif roll in taken and taken[roll].lower() == name.lower():
            check.status, check.message = "duplicate", f"Already in this class · skipped"
        elif roll in taken:
            check.status, check.message = "fix", f"Roll {roll} is already used by {taken[roll]}. Use a different number."
        elif roll in seen:
            check.status, check.message = "fix", f"Roll {roll} appears twice in the file (row {seen[roll]})."
        elif problem := _email_problem(email):
            check.status, check.message = "fix", problem
        seen.setdefault(roll, i)
        result.rows.append(check)
    return result


def add_student(tx: Tx, actor: Actor, *, class_id: int, name: str, roll_no: str, parent_email: str = "",
                consent_by: str = "", consent_method: str = "") -> dict:
    """Add one student and return their login details, including the one-time PIN (ST-1, ST-2)."""
    require(actor, "teacher", "inst_admin")
    require_class(tx, actor, class_id, write=True)
    check = check_rows(tx, class_id, [{"roll_no": roll_no, "name": name, "parent_email": parent_email}]).rows[0]
    if check.status != "ready":
        raise ValidationError(check.message)
    pin = security.new_pin()
    sid = tx.val("INSERT INTO users(institution_id, name, status) VALUES (%s,%s,'active') RETURNING id", [tx.inst, check.name])
    tx.exec("INSERT INTO user_roles(user_id, institution_id, role) VALUES (%s,%s,'student')", [sid, tx.inst])
    consent = bool(consent_by.strip())
    eid = tx.val("""INSERT INTO enrolments(institution_id, class_id, student_id, roll_no, pin_hash, parent_email, consent_status,
                    consent_by, consent_method, consent_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                 [tx.inst, class_id, sid, check.roll_no, security.hash_secret(pin), check.parent_email or None,
                  "recorded" if consent else "pending", consent_by.strip() or None, consent_method.strip() or None,
                  clock.now() if consent else None])
    audit.record(tx, "student.add", actor_id=actor.user_id, target=f"user:{sid}", detail={"class_id": class_id})
    return {"student_id": sid, "enrolment_id": eid, "name": check.name, "roll_no": check.roll_no, "pin": pin}


def import_rows(tx: Tx, actor: Actor, class_id: int, rows: list[dict[str, str]], *, skip_invalid: bool) -> ImportResult:
    """Add every ready row. Without skip_invalid, nothing is added while any row needs a fix (ST-1)."""
    require(actor, "teacher", "inst_admin")
    require_class(tx, actor, class_id, write=True)
    result = check_rows(tx, class_id, rows)
    if result.to_fix and not skip_invalid:
        raise ValidationError(f"Fix {result.to_fix} row{'s' if result.to_fix > 1 else ''} first, or skip them.")
    for r in result.rows:
        if r.status == "ready":
            result.added.append(add_student(tx, actor, class_id=class_id, name=r.name, roll_no=r.roll_no,
                                            parent_email=r.parent_email))
    return result


def list_students(tx: Tx, class_id: int) -> list[dict]:
    """Students in a class with consent and last login."""
    return tx.all("""SELECT e.id AS enrolment_id, u.id AS student_id, u.name, e.roll_no, e.parent_email, e.consent_status,
        e.status, u.last_login_at FROM enrolments e JOIN users u ON u.id=e.student_id
        WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""", [class_id])


def _enrolment(tx: Tx, actor: Actor, enrolment_id: int) -> dict:
    row = tx.one("SELECT id, class_id, student_id, roll_no FROM enrolments WHERE id=%s AND status='active'", [enrolment_id])
    if not row:
        raise NotFound("That student was not found.")
    require_class(tx, actor, row["class_id"], write=True)
    return row


def reset_pin(tx: Tx, actor: Actor, enrolment_id: int) -> str:
    """Give a student a new PIN and unlock their login immediately (ST-3)."""
    require(actor, "teacher", "inst_admin")
    row = _enrolment(tx, actor, enrolment_id)
    pin = security.new_pin()
    tx.exec("UPDATE enrolments SET pin_hash=%s WHERE id=%s", [security.hash_secret(pin), enrolment_id])
    label = tx.val("SELECT label FROM classes WHERE id=%s", [row["class_id"]])
    security.clear_login_failures(tx, f"pin:{tx.inst}:{label.upper()}:{row['roll_no']}")
    audit.record(tx, "student.pin_reset", actor_id=actor.user_id, target=f"user:{row['student_id']}")
    return pin


def record_consent(tx: Tx, actor: Actor, enrolment_id: int, *, parent_name: str, method: str) -> None:
    """Record verifiable parental consent; without it the account cannot be used (ST-4)."""
    require(actor, "teacher", "inst_admin")
    _enrolment(tx, actor, enrolment_id)
    if len(parent_name.strip()) < 2 or method not in ("signed_form", "in_person", "email_confirmation"):
        raise ValidationError("Enter the parent's name and how consent was given.")
    tx.exec("""UPDATE enrolments SET consent_status='recorded', consent_by=%s, consent_method=%s, consent_at=%s WHERE id=%s""",
            [parent_name.strip(), method, clock.now(), enrolment_id])
    audit.record(tx, "student.consent", actor_id=actor.user_id, target=f"enrolment:{enrolment_id}", detail={"method": method})


def deactivate(tx: Tx, actor: Actor, enrolment_id: int) -> int:
    """Remove a student's access while keeping their history (ST-5). Returns the student id."""
    require(actor, "teacher", "inst_admin")
    row = _enrolment(tx, actor, enrolment_id)
    tx.exec("UPDATE enrolments SET status='inactive', ended_at=%s WHERE id=%s", [clock.now(), enrolment_id])
    tx.exec("UPDATE users SET status='inactive' WHERE id=%s", [row["student_id"]])
    tx.exec("UPDATE sessions SET revoked=true WHERE user_id=%s", [row["student_id"]])
    audit.record(tx, "student.deactivate", actor_id=actor.user_id, target=f"user:{row['student_id']}")
    return row["student_id"]


def move(tx: Tx, actor: Actor, enrolment_id: int, *, to_class_id: int, new_roll_no: str) -> None:
    """Move a student to another class, keeping their history (ST-5)."""
    require(actor, "teacher", "inst_admin")
    row = _enrolment(tx, actor, enrolment_id)
    if not tx.val("SELECT 1 FROM classes WHERE id=%s AND status='active'", [to_class_id]):
        raise NotFound("That class was not found.")
    if new_roll_no.strip() in _taken_rolls(tx, to_class_id):
        raise Conflict(f"Roll {new_roll_no} is already used in that class.")
    full = tx.one("SELECT pin_hash, parent_email, consent_status, consent_by, consent_method, consent_at FROM enrolments WHERE id=%s",
                  [enrolment_id])
    tx.exec("UPDATE enrolments SET status='moved', ended_at=%s WHERE id=%s", [clock.now(), enrolment_id])
    tx.exec("""INSERT INTO enrolments(institution_id, class_id, student_id, roll_no, pin_hash, parent_email, consent_status,
               consent_by, consent_method, consent_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [tx.inst, to_class_id, row["student_id"], new_roll_no.strip(), full["pin_hash"], full["parent_email"],
             full["consent_status"], full["consent_by"], full["consent_method"], full["consent_at"]])
    audit.record(tx, "student.move", actor_id=actor.user_id, target=f"user:{row['student_id']}", detail={"to": to_class_id})
