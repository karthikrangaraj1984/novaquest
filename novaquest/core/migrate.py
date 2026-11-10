"""Create database roles and apply numbered SQL migrations."""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql

log = logging.getLogger("novaquest.migrate")
MIGRATIONS = Path(__file__).with_name("migrations")


def _parts(url: str) -> tuple[str, str, str]:
    u = urlparse(url)
    return u.username or "", u.password or "", (u.path or "/").lstrip("/")


def bootstrap(admin_url: str, owner_url: str, app_url: str, *, with_vector: bool | None = None) -> bool:
    """Create the owner and app roles and the database if missing (needs a superuser URL).

    pgvector is used when the server has it (with_vector=None) and skipped otherwise: it only powers the
    optional embedding search, and Windows builds of Postgres don't ship it. Returns whether it is enabled."""
    owner, owner_pw, dbname = _parts(owner_url)
    app, app_pw, _ = _parts(app_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for role, pw, extra in ((owner, owner_pw, "CREATEROLE"), (app, app_pw, "")):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone()
            stmt = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            conn.execute(sql.SQL(stmt + (" " + extra if extra else "")).format(sql.Identifier(role), sql.Literal(pw)))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [dbname]).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(owner)))
    admin_db_url = admin_url.rsplit("/", 1)[0] + "/" + dbname
    with psycopg.connect(admin_db_url, autocommit=True) as conn:
        if with_vector is None:
            with_vector = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone() is not None
        if with_vector:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        else:
            log.warning("pgvector is not installed on this Postgres server: embedding search is off, full-text search is used")
        conn.execute("CREATE EXTENSION IF NOT EXISTS ltree")
        conn.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(owner)))
    return bool(with_vector)


def migrate(owner_url: str, app_role: str) -> list[str]:
    """Apply pending migrations as the owner, then grant the app role its privileges."""
    applied: list[str] = []
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", [path.name])
            applied.append(path.name)
            log.info("applied migration %s", path.name)
        _ensure_embedding_column(conn)
        ident = sql.Identifier(app_role)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("REVOKE ALL ON schema_migrations FROM {}").format(ident))
    return applied


def has_embeddings(conn) -> bool:
    """True if source_passages has the pgvector embedding column."""
    return conn.execute("""SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'source_passages' AND column_name = 'embedding'""").fetchone() is not None


def _ensure_embedding_column(conn) -> None:
    """Add the embedding column once pgvector is enabled (for example, pgvector installed after the first setup)."""
    if has_embeddings(conn) or not conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone():
        return
    with conn.transaction():
        conn.execute("ALTER TABLE source_passages ADD COLUMN embedding vector(768)")
        conn.execute("CREATE INDEX source_passages_emb_idx ON source_passages USING hnsw (embedding vector_cosine_ops)")
    log.info("pgvector enabled: added source_passages.embedding")
