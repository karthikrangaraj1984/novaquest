"""Audit trail of logins, account changes, overrides and releases."""
from __future__ import annotations

from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx, jsonb


def record(tx: Tx, action: str, *, actor_id: int | None, target: str | None = None,
           detail: dict[str, Any] | None = None, institution_id: int | None = None) -> None:
    """Write one audit event in the caller's transaction."""
    tx.exec(
        "INSERT INTO audit_events(institution_id, actor_id, action, target, detail, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        [institution_id if institution_id is not None else tx.institution_id, actor_id, action, target,
         jsonb(detail or {}), clock.now()],
    )
