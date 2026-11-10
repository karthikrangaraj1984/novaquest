"""Application settings, read once from environment variables."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def default_data_dir(env: dict | None = None, windows: bool = os.name == "nt") -> Path:
    """Where uploaded books, rendered book pages and other saved files live: NQ_DATA_DIR if set; on Windows the
    NovaQuest folder in the user's local app data (next to the database), so files survive every upgrade (each version
    unzips into a new folder); elsewhere ./var."""
    env = os.environ if env is None else env
    named = (env.get("NQ_DATA_DIR") or "").strip()
    if named:
        return Path(named)
    local = (env.get("LOCALAPPDATA") or "").strip()
    if windows and local:
        return Path(local) / "NovaQuest" / "data"
    return ROOT / "var"


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime settings. Construct with `load_settings()`."""

    database_url: str
    app_database_url: str
    secret_key: str
    base_url: str
    timezone: str
    ai_mode: str
    anthropic_api_key: str
    anthropic_base_url: str
    model_fast: str
    model_strong: str
    ai_timeout_s: float
    ai_daily_token_cap: int
    embed_url: str
    embed_model: str
    embed_dims: int
    smtp_host: str
    smtp_port: int
    mail_from: str
    data_dir: Path
    upload_max_bytes: int
    pool_min: int
    pool_max: int
    secure_cookies: bool
    login_max_failures: int
    login_lock_minutes: int
    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    """Build settings from the environment with safe local defaults."""
    api_key = _env("ANTHROPIC_API_KEY")
    ai_mode = _env("NQ_AI_MODE") or ("claude" if api_key else "offline")
    owner_url = _env("NQ_DATABASE_URL", "postgresql://nq_owner:nq_owner@127.0.0.1:54329/novaquest")
    return Settings(
        database_url=owner_url,
        app_database_url=_env("NQ_APP_DATABASE_URL", owner_url.replace("nq_owner:nq_owner", "nq_app:nq_app")),
        secret_key=_env("NQ_SECRET_KEY") or secrets.token_hex(32),
        base_url=_env("NQ_BASE_URL", "https://localhost:8443"),
        timezone=_env("NQ_TIMEZONE", "Asia/Kolkata"),
        ai_mode=ai_mode,
        anthropic_api_key=api_key,
        anthropic_base_url=_env("NQ_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        model_fast=_env("NQ_MODEL_FAST", "claude-haiku-4-5-20251001"),
        model_strong=_env("NQ_MODEL_STRONG", "claude-sonnet-5-5"),
        ai_timeout_s=float(_env("NQ_AI_TIMEOUT_S", "20")),
        ai_daily_token_cap=_env_int("NQ_AI_DAILY_TOKEN_CAP", 2_000_000),
        embed_url=_env("NQ_EMBED_URL"),
        embed_model=_env("NQ_EMBED_MODEL", "nomic-embed-text"),
        embed_dims=_env_int("NQ_EMBED_DIMS", 768),
        smtp_host=_env("NQ_SMTP_HOST", "127.0.0.1"),
        smtp_port=_env_int("NQ_SMTP_PORT", 1025),
        mail_from=_env("NQ_MAIL_FROM", "NovaQuest <no-reply@novaquest.local>"),
        data_dir=default_data_dir(),
        upload_max_bytes=_env_int("NQ_UPLOAD_MAX_BYTES", 10 * 1024 * 1024),
        pool_min=_env_int("NQ_POOL_MIN", 2),
        pool_max=_env_int("NQ_POOL_MAX", 12),
        secure_cookies=_env("NQ_SECURE_COOKIES", "1") == "1",
        login_max_failures=_env_int("NQ_LOGIN_MAX_FAILURES", 5),
        login_lock_minutes=_env_int("NQ_LOGIN_LOCK_MINUTES", 5),
    )
