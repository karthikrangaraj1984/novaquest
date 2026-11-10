"""Sign-in for staff (email + password) and Grade 8 students (school code + class + roll + PIN)."""
from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import timedelta

from novaquest.core import audit, clock, security
from novaquest.core.config import Settings
from novaquest.core.db import Db
from novaquest.core.errors import Forbidden, ValidationError
from novaquest.services.access import Actor

SESSION_HOURS = 12
_ROLE_ORDER = ("platform_admin", "inst_admin", "leader", "teacher", "student")


@dataclass(frozen=True, slots=True)
class Session:
    """A signed-in session."""

    token: str
    csrf: str
    actor: Actor


def _roles(db: Db, user_id: int, institution_id: int | None) -> tuple[str, ...]:
    ctx = db.tenant(institution_id) if institution_id else db.platform()
    with ctx as tx:
        roles = [r["role"] for r in tx.all("SELECT role FROM user_roles WHERE user_id=%s", [user_id])]
    return tuple(sorted(roles, key=_ROLE_ORDER.index))


def _start(db: Db, user_id: int, institution_id: int | None, role: str) -> Session:
    token, csrf = security.new_token(), security.new_token(16)
    now = clock.now()
    with db.anonymous() as tx:
        tx.exec("""INSERT INTO sessions(token_hash, user_id, institution_id, active_role, csrf_token, created_at, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                [security.token_hash(token), user_id, institution_id, role, csrf, now, now + timedelta(hours=SESSION_HOURS)])
    actor = load_actor(db, user_id, institution_id, role)
    return Session(token, csrf, actor)


def load_actor(db: Db, user_id: int, institution_id: int | None, role: str) -> Actor:
    """Build the Actor for a user and active role."""
    ctx = db.tenant(institution_id) if institution_id else db.platform()
    with ctx as tx:
        name = tx.val("SELECT name FROM users WHERE id=%s", [user_id]) or ""
    return Actor(user_id, institution_id, role, _roles(db, user_id, institution_id), name)


def login_email(db: Db, settings: Settings, email: str, password: str) -> Session:
    """Staff and NovaQuest admin sign-in."""
    key = "email:" + email.strip().lower()
    with db.anonymous() as tx:
        security.assert_not_locked(tx, key)
        row = tx.one("SELECT * FROM nq_user_by_email(%s)", [email.strip()])
    if not row or row["status"] != "active" or not security.verify_secret(password, row["password_hash"]):
        with db.anonymous() as tx:
            security.record_login_failure(tx, key, settings.login_max_failures, settings.login_lock_minutes)
        raise ValidationError("That email and password don't match. Check them and try again.")
    with db.anonymous() as tx:
        security.clear_login_failures(tx, key)
    roles = _roles(db, row["user_id"], row["institution_id"])
    if not roles or roles == ("student",):
        raise Forbidden("This account can't sign in here.")
    _touch(db, row["user_id"], row["institution_id"], "login.email")
    return _start(db, row["user_id"], row["institution_id"], roles[0])


def class_key(label: str) -> str:
    """A class as students type it: '8 b', '8-B' and '8B' are the same class."""
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def login_pin(db: Db, settings: Settings, school_code: str, class_label: str, roll_no: str, pin: str) -> Session:
    """Grade 8 student sign-in with school code, class, roll number and PIN (no email needed)."""
    with db.anonymous() as tx:
        inst = tx.val("SELECT nq_institution_by_code(%s)", [school_code.strip()])
    generic = ValidationError("Those details don't match. Check your login slip, or ask your teacher.")
    if not inst:
        raise generic
    klass = class_key(class_label)
    key = f"pin:{inst}:{klass}:{roll_no.strip()}"
    with db.anonymous() as tx:
        security.assert_not_locked(tx, key)
    with db.tenant(inst) as tx:
        row = tx.one("""SELECT e.student_id, e.pin_hash, e.consent_status, u.status FROM enrolments e
            JOIN classes c ON c.id=e.class_id JOIN users u ON u.id=e.student_id
            WHERE regexp_replace(upper(c.label), '[^A-Z0-9]', '', 'g')=%s AND c.status='active' AND e.roll_no=%s
              AND e.status='active'""", [klass, roll_no.strip()])
    if not row or not security.verify_secret(pin.strip(), row["pin_hash"]):
        with db.anonymous() as tx:
            security.record_login_failure(tx, key, settings.login_max_failures, settings.login_lock_minutes)
        raise generic
    if row["consent_status"] != "recorded":
        raise Forbidden("Your account isn't active yet. Your teacher needs your parent's consent first.")
    if row["status"] != "active":
        raise Forbidden("Your account is not active. Please ask your teacher.")
    with db.anonymous() as tx:
        security.clear_login_failures(tx, key)
    _touch(db, row["student_id"], inst, "login.pin")
    return _start(db, row["student_id"], inst, "student")


def _touch(db: Db, user_id: int, institution_id: int | None, action: str) -> None:
    ctx = db.tenant(institution_id) if institution_id else db.platform()
    with ctx as tx:
        tx.exec("UPDATE users SET last_login_at=%s WHERE id=%s", [clock.now(), user_id])
        audit.record(tx, action, actor_id=user_id, target=f"user:{user_id}", institution_id=institution_id)


def get_session(db: Db, token: str | None) -> Session | None:
    """Return the live session for a cookie token, or None."""
    if not token:
        return None
    with db.anonymous() as tx:  # one round trip: session, name and roles together
        row = tx.one("SELECT * FROM nq_session(%s, %s)", [security.token_hash(token), clock.now()])
    if not row or row["active_role"] not in (row["roles"] or []):
        return None
    roles = tuple(sorted(row["roles"], key=_ROLE_ORDER.index))
    return Session(token, row["csrf_token"], Actor(row["user_id"], row["institution_id"], row["active_role"], roles, row["name"]))


def switch_role(db: Db, session: Session, role: str) -> Session:
    """Change the active role for a person who holds more than one (e.g. HOD who also teaches)."""
    if role not in session.actor.roles:
        raise Forbidden("You don't have that role.")
    with db.anonymous() as tx:
        tx.exec("UPDATE sessions SET active_role=%s WHERE token_hash=%s", [role, security.token_hash(session.token)])
    return Session(session.token, session.csrf, load_actor(db, session.actor.user_id, session.actor.institution_id, role))


def logout(db: Db, token: str) -> None:
    """Revoke a session."""
    with db.anonymous() as tx:
        tx.exec("UPDATE sessions SET revoked=true WHERE token_hash=%s", [security.token_hash(token)])


def revoke_user_sessions(db: Db, user_id: int) -> None:
    """Sign a user out everywhere (after deactivation or password reset)."""
    with db.anonymous() as tx:
        tx.exec("UPDATE sessions SET revoked=true WHERE user_id=%s", [user_id])


def accept_invite(db: Db, token: str, password: str) -> int:
    """Staff set their password from the invite link (ON-3). Returns the user id."""
    problem = security.check_password_strength(password)
    if problem:
        raise ValidationError(problem, field="password")
    with db.anonymous() as tx:
        row = tx.one("SELECT * FROM nq_user_by_invite(%s)", [security.token_hash(token)])
    if not row:
        raise ValidationError("This invite link is not valid any more. Ask your admin for a new one.")
    ctx = db.tenant(row["institution_id"]) if row["institution_id"] else db.platform()
    with ctx as tx:
        tx.exec("UPDATE users SET password_hash=%s, status='active', invite_token_hash=NULL WHERE id=%s",
                [security.hash_secret(password), row["user_id"]])
        audit.record(tx, "user.invite_accepted", actor_id=row["user_id"], target=f"user:{row['user_id']}",
                     institution_id=row["institution_id"])
    return row["user_id"]
