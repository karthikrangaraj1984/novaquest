"""Postgres-backed job queue: enqueue with NOTIFY, claim with FOR UPDATE SKIP LOCKED."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Db, Tx, jsonb

CHANNEL = "nq_jobs"


def enqueue(tx: Tx, kind: str, payload: dict[str, Any] | None = None, *, run_at: datetime | None = None,
            dedupe_key: str | None = None, institution_id: int | None = None) -> None:
    """Queue a job (ignored if a job with the same dedupe key exists) and wake the worker."""
    tx.exec("""INSERT INTO jobs(kind, institution_id, payload, run_at, dedupe_key) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (dedupe_key) DO NOTHING""",
            [kind, institution_id if institution_id is not None else tx.institution_id, jsonb(payload or {}),
             run_at or clock.now(), dedupe_key])
    tx.exec(f"NOTIFY {CHANNEL}")


def claim(db: Db, limit: int = 10) -> list[dict[str, Any]]:
    """Claim due jobs for this worker; other workers skip the locked rows."""
    with db.anonymous() as tx:
        return tx.all("""UPDATE jobs SET status='running', attempts=attempts+1 WHERE id IN (
                SELECT id FROM jobs WHERE status='queued' AND run_at <= %s ORDER BY run_at, id
                LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING id, kind, institution_id, payload, attempts""",
                      [clock.now(), limit])


def requeue_interrupted(db: Db) -> int:
    """Put jobs a stopped worker left 'running' back in the queue (one worker runs at a time, so at its start any
    running job was interrupted, e.g. by a restart or an upgrade). Returns how many."""
    with db.anonymous() as tx:
        return int(tx.val("""WITH r AS (UPDATE jobs SET status='queued', run_at=%s WHERE status='running' RETURNING 1)
                             SELECT count(*) FROM r""", [clock.now()]) or 0)


def finish(db: Db, job_id: int, error: str | None = None, retry_at: datetime | None = None) -> None:
    """Mark a job done, failed, or queued again for a retry."""
    with db.anonymous() as tx:
        if error and retry_at:
            tx.exec("UPDATE jobs SET status='queued', run_at=%s, last_error=%s WHERE id=%s", [retry_at, error[:500], job_id])
        else:
            tx.exec("UPDATE jobs SET status=%s, last_error=%s, finished_at=%s WHERE id=%s",
                    ["failed" if error else "done", (error or None) and error[:500], clock.now(), job_id])
