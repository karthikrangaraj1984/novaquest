"""Background worker: assignment state changes, reminders, digests, expiry summaries, auto-submit
of overdue attempts and email sending. Run with `python -m novaquest.worker`."""
from __future__ import annotations

import logging
import os
import signal
import time
from datetime import timedelta
from typing import Any, Callable

import psycopg

from novaquest.ai.factory import get_engine
from novaquest.core import clock
from novaquest.core.config import Settings, load_settings
from novaquest.core.db import Db, init_db
from novaquest.services import (assignments, attempts, books, briefing, figures, jobs, notify, paper, prep, principal, privacy,
                                review)

log = logging.getLogger("novaquest.worker")
TICK_SECONDS = float(os.environ.get("NQ_TICK_SECONDS", "30"))


def _assignment_closed(db: Db, s: Settings, job: dict) -> None:
    with db.tenant(job["institution_id"]) as tx:
        assignments.sync_states(tx)
        review.expiry_summary(tx, job["payload"]["assignment_id"])


def _reminder(db: Db, s: Settings, job: dict) -> None:
    with db.tenant(job["institution_id"]) as tx:
        assignments.sync_states(tx)
        review.send_reminders(tx, job["payload"]["assignment_id"])


def _digest(db: Db, s: Settings, job: dict) -> None:
    with db.tenant(job["institution_id"]) as tx:
        review.completion_digest(tx, job["payload"]["assignment_id"])


def _deadline(db: Db, s: Settings, job: dict) -> None:
    with db.tenant(job["institution_id"]) as tx:
        att = tx.one("SELECT status, deadline_at FROM attempts WHERE id=%s", [job["payload"]["attempt_id"]])
    if att and att["status"] == "in_progress" and att["deadline_at"] <= clock.now():
        attempts.submit(db, get_engine(s, db, job["institution_id"]), None, job["payload"]["attempt_id"],
                        institution_id=job["institution_id"], auto=True)


def _book_read(db: Db, s: Settings, job: dict) -> None:
    books.read_book(db, get_engine(s, db, job["institution_id"]), job["institution_id"], job["payload"]["syllabus_id"],
                    books.storage_dir(s.data_dir))


def _book_lessons(db: Db, s: Settings, job: dict) -> None:
    books.draft_lessons(db, get_engine(s, db, job["institution_id"]), job["institution_id"], job["payload"]["syllabus_id"],
                        skip=job["payload"].get("skip"), failed_ids=job["payload"].get("failed"))


def _book_escape(db: Db, s: Settings, job: dict) -> None:
    books.draft_escape(db, get_engine(s, db, job["institution_id"]), job["institution_id"], job["payload"]["syllabus_id"],
                       skip=job["payload"].get("skip"), failed=job["payload"].get("failed"))


def _book_refresh(db: Db, s: Settings, job: dict) -> None:
    books.refresh_text(db, job["institution_id"], job["payload"]["syllabus_id"], books.storage_dir(s.data_dir))


def _paper_read(db: Db, s: Settings, job: dict) -> None:
    paper.read(db, get_engine(s, db, job["institution_id"]), job["institution_id"], job["payload"]["scan_id"],
               paper.storage_dir(s.data_dir))


def _book_figures(db: Db, s: Settings, job: dict) -> None:
    figures.warm(db, job["institution_id"], job["payload"]["syllabus_id"], s.data_dir)


def _unit_pack(db: Db, s: Settings, job: dict) -> None:
    prep.write_with_claude(db, get_engine(s, db, job["institution_id"]), job["institution_id"], job["payload"]["chapter_id"])


HANDLERS: dict[str, Callable[[Db, Settings, dict], None]] = {
    "assignment_closed": _assignment_closed, "assignment_reminder": _reminder,
    "completion_digest": _digest, "attempt_deadline": _deadline,
    books.READ_JOB: _book_read, books.LESSONS_JOB: _book_lessons, books.ESCAPE_JOB: _book_escape,
    books.REFRESH_JOB: _book_refresh, paper.JOB: _paper_read, prep.JOB: _unit_pack,
    figures.JOB: _book_figures}


def tick(db: Db, settings: Settings) -> dict[str, int]:
    """Periodic housekeeping that does not depend on any single job."""
    with db.platform() as tx:
        closed = assignments.sync_states(tx)
        due = tx.all("""SELECT id, institution_id FROM assignments WHERE state='open' AND reminder_sent_at IS NULL
                        AND expires_at - interval '24 hours' <= %s""", [clock.now()])
        unclosed = tx.all("""SELECT id, institution_id FROM assignments WHERE state='closed' AND closed_notified_at IS NULL""")
    for a in unclosed:
        with db.tenant(a["institution_id"]) as tx:
            review.expiry_summary(tx, a["id"])
    for a in due:
        with db.tenant(a["institution_id"]) as tx:
            review.send_reminders(tx, a["id"])
    expired = attempts.expire_overdue(db, lambda inst: get_engine(settings, db, inst))
    weekly = weekly_summaries(db)
    briefings = weekly_briefings(db)
    try:
        retained = privacy.daily_retention(db, paper.storage_dir(settings.data_dir))
    except Exception:  # the clean-up must not stop the rest of the tick
        log.exception("data retention failed")
        retained = 0
    sent = notify.send_pending(db, settings)
    return {"closed": len(closed), "reminded": len(due), "expired": expired, "weekly": weekly, "briefings": briefings, "retention": retained, "emails": sent}


def weekly_summaries(db: Db) -> int:
    """Each Monday from 7 am (Indian time), send every principal their school's weekly summary once."""
    if principal.week_key() is None:
        return 0
    with db.platform() as tx:
        schools = [r["id"] for r in tx.all("""SELECT DISTINCT u.institution_id AS id FROM users u
            JOIN user_roles r ON r.user_id=u.id AND r.role='leader' WHERE u.status='active' AND u.institution_id IS NOT NULL""")]
    n = 0
    for inst in schools:
        try:
            with db.tenant(inst) as tx:
                n += principal.send_weekly(tx)
        except Exception:  # one school's problem must not stop the others
            log.exception("weekly summary failed for institution %s", inst)
    return n


def weekly_briefings(db: Db) -> int:
    """Each Monday from 7 am (Indian time), send every teacher the weekly briefing for each of their classes once."""
    if principal.week_key() is None:
        return 0
    with db.platform() as tx:
        schools = [r["id"] for r in tx.all("""SELECT DISTINCT ta.institution_id AS id FROM teaching_assignments ta
            JOIN users u ON u.id=ta.teacher_id WHERE u.status='active'""")]
    n = 0
    for inst in schools:
        try:
            with db.tenant(inst) as tx:
                n += briefing.send_weekly(tx)
        except Exception:  # one school's problem must not stop the others
            log.exception("weekly briefing failed for institution %s", inst)
    return n


def run_due(db: Db, settings: Settings, limit: int = 50) -> int:
    """Run every due job once. Failed jobs retry with backoff up to 5 times. Returns jobs run."""
    ran = 0
    for job in jobs.claim(db, limit):
        handler = HANDLERS.get(job["kind"])
        try:
            if handler is None:
                raise ValueError(f"unknown job kind {job['kind']}")
            handler(db, settings, job)
            jobs.finish(db, job["id"])
        except Exception as exc:  # a failed job must not stop the worker
            log.exception("job %s (%s) failed", job["id"], job["kind"])
            retry = clock.now() + timedelta(seconds=30 * 2 ** job["attempts"]) if job["attempts"] < 5 else None
            jobs.finish(db, job["id"], str(exc), retry)
        ran += 1
    return ran


def main() -> None:
    """Run forever: wake on NOTIFY or every few seconds, and tick every TICK_SECONDS."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    db = init_db(settings.app_database_url, 1, 6)  # lesson drafting uses a few connections at once
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    listener = psycopg.connect(settings.app_database_url, autocommit=True)
    listener.execute(f"LISTEN {jobs.CHANNEL}")
    last_tick = 0.0
    back = jobs.requeue_interrupted(db)
    log.info("worker started%s", f"; {back} interrupted job(s) queued again" if back else "")
    while not stop["flag"]:
        if time.monotonic() - last_tick >= TICK_SECONDS:
            try:
                log.info("tick %s", tick(db, settings))
            except Exception:
                log.exception("tick failed")
            last_tick = time.monotonic()
        run_due(db, settings)
        for _ in listener.notifies(timeout=5.0, stop_after=1):
            pass
    listener.close()
    db.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
