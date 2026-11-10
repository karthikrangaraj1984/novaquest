"""In-app notifications and the email outbox (sent by the worker through SMTP / Mailpit)."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from novaquest.core import clock
from novaquest.core.config import Settings
from novaquest.core.db import Db, Tx

log = logging.getLogger("novaquest.notify")


def notify(tx: Tx, user_id: int, kind: str, title: str, body: str, link: str | None, dedupe_key: str) -> None:
    """Create or refresh one in-app notification. Re-sending the same key updates it and marks it unread,
    so digests like '28 of 32 completed' stay a single notification."""
    now = clock.now()
    tx.exec("""INSERT INTO notifications(institution_id, user_id, kind, title, body, link, dedupe_key, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (user_id, dedupe_key) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body,
                 link=EXCLUDED.link, read_at=NULL, updated_at=EXCLUDED.updated_at""",
            [tx.inst, user_id, kind, title, body, link, dedupe_key, now, now])


def queue_email(tx: Tx, to_addr: str | None, subject: str, body: str, dedupe_key: str,
                send_after: datetime | None = None) -> bool:
    """Queue an email once per dedupe key. Returns False if there is no address."""
    if not to_addr:
        return False
    tx.exec("""INSERT INTO email_outbox(institution_id, to_addr, subject, body, dedupe_key, send_after, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING""",
            [tx.inst, to_addr, subject, body, dedupe_key, send_after or clock.now(), clock.now()])
    return True


def unread(tx: Tx, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Latest notifications for a user, unread first."""
    return tx.all("""SELECT id, kind, title, body, link, read_at, updated_at FROM notifications WHERE user_id=%s
                     ORDER BY (read_at IS NULL) DESC, updated_at DESC LIMIT %s""", [user_id, limit])


def unread_count(tx: Tx, user_id: int) -> int:
    """How many unread notifications a user has."""
    return int(tx.val("SELECT count(*) FROM notifications WHERE user_id=%s AND read_at IS NULL", [user_id]) or 0)


def mark_read(tx: Tx, user_id: int) -> None:
    """Mark all of a user's notifications as read."""
    tx.exec("UPDATE notifications SET read_at=%s WHERE user_id=%s AND read_at IS NULL", [clock.now(), user_id])


def send_pending(db: Db, settings: Settings, limit: int = 50) -> int:
    """Send queued emails through SMTP. Failures retry up to 5 times. Returns how many were sent."""
    with db.platform() as tx:
        rows = tx.all("""SELECT id, to_addr, subject, body, attempts FROM email_outbox
                         WHERE status='pending' AND send_after <= %s ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
                      [clock.now(), limit])
        if not rows:
            return 0
        sent = 0
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                for r in rows:
                    msg = EmailMessage()
                    msg["From"], msg["To"], msg["Subject"] = settings.mail_from, r["to_addr"], r["subject"]
                    msg.set_content(r["body"])
                    smtp.send_message(msg)
                    tx.exec("UPDATE email_outbox SET status='sent', sent_at=%s, attempts=attempts+1 WHERE id=%s",
                            [clock.now(), r["id"]])
                    sent += 1
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("email sending failed: %s", exc)
            for r in rows[sent:]:
                tx.exec("""UPDATE email_outbox SET attempts=attempts+1, send_after=%s,
                           status=CASE WHEN attempts+1 >= 5 THEN 'failed' ELSE 'pending' END WHERE id=%s""",
                        [clock.now() + timedelta(minutes=2), r["id"]])
        return sent
