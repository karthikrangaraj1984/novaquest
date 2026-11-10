"""Database access. All institution data goes through `Db.tenant()`, which sets the
row-level-security context so Postgres itself hides other institutions' rows."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("novaquest.db")

Row = dict[str, Any]


class Tx:
    """A connection inside one transaction, with small query helpers."""

    __slots__ = ("conn", "institution_id", "platform")

    def __init__(self, conn: psycopg.Connection, institution_id: int | None, platform: bool) -> None:
        self.conn = conn
        self.institution_id = institution_id
        self.platform = platform

    def all(self, sql: str, params: Sequence[Any] | dict | None = None) -> list[Row]:
        """Run a query and return every row as a dict."""
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict | None = None) -> Row | None:
        """Run a query and return the first row, or None."""
        return self.conn.execute(sql, params).fetchone()

    def val(self, sql: str, params: Sequence[Any] | dict | None = None) -> Any:
        """Run a query and return the first column of the first row, or None."""
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else next(iter(row.values()))

    def exec(self, sql: str, params: Sequence[Any] | dict | None = None) -> int:
        """Run a statement and return the number of affected rows."""
        return self.conn.execute(sql, params).rowcount

    def many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one statement for many parameter rows (batched by psycopg)."""
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)

    @property
    def inst(self) -> int:
        """The institution in scope; raises if this is not a tenant transaction."""
        if self.institution_id is None:
            raise RuntimeError("No institution in scope for this transaction")
        return self.institution_id


def jsonb(value: Any) -> Jsonb:
    """Wrap a Python value for a jsonb parameter."""
    return Jsonb(value)


class Db:
    """Connection pool plus scoped transaction helpers."""

    def __init__(self, url: str, min_size: int = 2, max_size: int = 12) -> None:
        self.url = url
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": 5},
            max_idle=300,
            timeout=15,
            open=False,
            name="nq",
        )

    def open(self) -> None:
        """Open the pool (idempotent) and wait until it is usable."""
        self.pool.open(wait=True, timeout=30)

    def close(self) -> None:
        """Close every pooled connection."""
        self.pool.close()

    @contextmanager
    def _tx(self, institution_id: int | None, platform: bool) -> Iterator[Tx]:
        with self.pool.connection() as conn:
            with conn.transaction():
                if platform:
                    conn.execute("SELECT set_config('nq.platform', 'on', true)")
                if institution_id is not None:
                    conn.execute("SELECT set_config('nq.institution_id', %s, true)", [str(int(institution_id))])
                yield Tx(conn, institution_id, platform)

    def tenant(self, institution_id: int) -> "contextmanager[Tx]":
        """Transaction that can see only one institution's rows."""
        if not institution_id:
            raise ValueError("institution_id is required")
        return self._tx(institution_id, False)

    def platform(self) -> "contextmanager[Tx]":
        """Transaction for NovaQuest platform work (all institutions). Use sparingly."""
        return self._tx(None, True)

    def anonymous(self) -> "contextmanager[Tx]":
        """Transaction with no tenant: sees only shared tables (syllabus, sessions, jobs)."""
        return self._tx(None, False)


_db: Db | None = None


def init_db(url: str, min_size: int = 2, max_size: int = 12) -> Db:
    """Create and open the process-wide pool."""
    global _db
    if _db is not None:
        _db.close()
    _db = Db(url, min_size, max_size)
    _db.open()
    return _db


def get_db() -> Db:
    """Return the process-wide pool."""
    if _db is None:
        raise RuntimeError("Database not initialised; call init_db() first")
    return _db
