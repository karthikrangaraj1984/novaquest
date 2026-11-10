"""Who may do what. Every service checks access itself, not just the web route."""
from __future__ import annotations

from dataclasses import dataclass

from novaquest.core.db import Tx
from novaquest.core.errors import Forbidden

STAFF_ROLES = ("inst_admin", "leader", "teacher")


@dataclass(frozen=True, slots=True)
class Actor:
    """The signed-in user making a request."""

    user_id: int
    institution_id: int | None
    role: str
    roles: tuple[str, ...]
    name: str

    def has(self, *roles: str) -> bool:
        """True if the active role is one of `roles`."""
        return self.role in roles


def require(actor: Actor, *roles: str) -> None:
    """Raise Forbidden unless the actor's active role is allowed."""
    if actor.role not in roles:
        raise Forbidden("You don't have access to this page.")


def teacher_class_ids(tx: Tx, user_id: int) -> list[int]:
    """Classes a teacher is mapped to (ON-4)."""
    return [r["class_id"] for r in tx.all("SELECT DISTINCT class_id FROM teaching_assignments WHERE teacher_id=%s", [user_id])]


def visible_class_ids(tx: Tx, actor: Actor) -> list[int]:
    """Classes the actor may see: own classes for teachers, all for leaders and admins."""
    if actor.role in ("leader", "inst_admin"):
        return [r["id"] for r in tx.all("SELECT id FROM classes WHERE status='active' ORDER BY label")]
    if actor.role == "teacher":
        return teacher_class_ids(tx, actor.user_id)
    return []


def require_class(tx: Tx, actor: Actor, class_id: int, *, write: bool = False) -> None:
    """Raise Forbidden unless the actor teaches (or, read-only, leads) this class."""
    if actor.role == "teacher" and class_id in teacher_class_ids(tx, actor.user_id):
        return
    if actor.role == "inst_admin":
        return
    if actor.role == "leader" and not write:
        return
    raise Forbidden("This class is not one of yours.")


def student_class_id(tx: Tx, student_id: int) -> int | None:
    """The student's current class."""
    return tx.val("SELECT class_id FROM enrolments WHERE student_id=%s AND status='active' ORDER BY started_at DESC LIMIT 1",
                  [student_id])
