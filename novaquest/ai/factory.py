"""Pick the AI engine for a request and record usage against the institution's daily cap."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

from novaquest.ai.claude import ClaudeEngine
from novaquest.ai.offline import OfflineEngine
from novaquest.ai.provider import ClaudeProvider, LLMProvider, LLMResult
from novaquest.core import clock
from novaquest.core.config import Settings
from novaquest.core.db import Db

log = logging.getLogger("novaquest.ai")

# Rough cost per million tokens in USD, for the admin cost dashboard (configurable later).
PRICE = {"fast": (1.0, 5.0), "strong": (3.0, 15.0)}

_provider_override: LLMProvider | None = None


def set_provider(provider: LLMProvider | None) -> None:
    """Inject a provider (tests use a fake Claude server through this)."""
    global _provider_override
    _provider_override = provider


def tokens_used_today(db: Db, institution_id: int) -> int:
    """Total tokens this institution has used today."""
    with db.tenant(institution_id) as tx:
        return int(tx.val("""SELECT coalesce(sum(input_tokens + output_tokens), 0) FROM ai_usage
                             WHERE institution_id=%s AND day=%s""", [institution_id, clock.now().date()]) or 0)


def usage_hook(db: Db, institution_id: int) -> Callable[[str, LLMResult | None, bool], None]:
    """Return a callback that records each model call in its own short transaction."""
    def record(tier: str, res: LLMResult | None, fallback: bool) -> None:
        try:
            with db.tenant(institution_id) as tx:
                tx.exec("""INSERT INTO ai_usage(institution_id, day, tier, calls, input_tokens, output_tokens,
                               cache_read_tokens, fallbacks) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (institution_id, day, tier) DO UPDATE SET calls = ai_usage.calls + EXCLUDED.calls,
                               input_tokens = ai_usage.input_tokens + EXCLUDED.input_tokens,
                               output_tokens = ai_usage.output_tokens + EXCLUDED.output_tokens,
                               cache_read_tokens = ai_usage.cache_read_tokens + EXCLUDED.cache_read_tokens,
                               fallbacks = ai_usage.fallbacks + EXCLUDED.fallbacks""",
                        [institution_id, clock.now().date(), tier, 0 if res is None else 1,
                         res.input_tokens if res else 0, res.output_tokens if res else 0,
                         res.cache_read_tokens if res else 0, 1 if fallback else 0])
        except Exception as exc:  # usage accounting must never break a student's request
            log.warning("could not record AI usage: %s", exc)
    return record


def get_engine(settings: Settings, db: Db, institution_id: int) -> ClaudeEngine | OfflineEngine:
    """Claude when configured and under today's cap; otherwise the offline engine."""
    provider = _provider_override
    if provider is None and settings.ai_mode == "claude" and settings.anthropic_api_key:
        provider = ClaudeProvider(settings.anthropic_api_key, settings.anthropic_base_url, settings.model_fast,
                                  settings.model_strong, settings.ai_timeout_s)
    if provider is None:
        return OfflineEngine()
    with db.tenant(institution_id) as tx:
        cap = tx.val("SELECT ai_daily_token_cap FROM institutions WHERE id=%s", [institution_id]) or settings.ai_daily_token_cap
    if tokens_used_today(db, institution_id) >= cap:
        log.warning("institution %s reached its daily AI cap; using offline engine", institution_id)
        usage_hook(db, institution_id)("capped", None, True)
        return OfflineEngine()
    return ClaudeEngine(provider, usage_hook(db, institution_id))


def cost_report(db: Db) -> list[dict]:
    """Per-institution AI usage and estimated cost for the NovaQuest admin (last 30 days)."""
    with db.platform() as tx:
        rows = tx.all("""SELECT i.name, u.tier, sum(u.calls) calls, sum(u.input_tokens) inp, sum(u.output_tokens) outp,
                sum(u.cache_read_tokens) cached, sum(u.fallbacks) fallbacks
            FROM ai_usage u JOIN institutions i ON i.id=u.institution_id
            WHERE u.day >= (now() - interval '30 days')::date GROUP BY i.name, u.tier ORDER BY i.name, u.tier""")
    for r in rows:
        pin, pout = PRICE.get(r["tier"], (0.0, 0.0))
        r["cost_usd"] = round((int(r["inp"]) * pin + int(r["outp"]) * pout) / 1_000_000, 4)
    return rows


def school_usage(db: Db, settings: Settings, institution_id: int) -> dict:
    """The school admin's view of Claude use: today's tokens against the daily limit, and the last 30 days' calls,
    tokens, estimated cost and how often the offline engine stood in (Claude busy, failed, or the limit reached)."""
    with db.tenant(institution_id) as tx:
        cap = tx.val("SELECT ai_daily_token_cap FROM institutions WHERE id=%s", [institution_id]) or settings.ai_daily_token_cap
        rows = tx.all("""SELECT tier, sum(calls) calls, sum(input_tokens) inp, sum(output_tokens) outp, sum(fallbacks) fallbacks
                         FROM ai_usage WHERE institution_id=%s AND day > %s GROUP BY tier""",
                      [institution_id, clock.now().date() - timedelta(days=30)])
    today = tokens_used_today(db, institution_id)
    cost = sum((int(r["inp"] or 0) * PRICE.get(r["tier"], (0, 0))[0] + int(r["outp"] or 0) * PRICE.get(r["tier"], (0, 0))[1])
               / 1_000_000 for r in rows)
    calls = sum(int(r["calls"] or 0) for r in rows if r["tier"] != "capped")
    return {"configured": settings.ai_mode == "claude" and bool(settings.anthropic_api_key), "today": today, "cap": int(cap),
            "today_pct": min(100, round(100 * today / cap)) if cap else 0, "calls": calls,
            "tokens": sum(int(r["inp"] or 0) + int(r["outp"] or 0) for r in rows),
            "fallbacks": sum(int(r["fallbacks"] or 0) for r in rows), "cost_usd": round(cost, 2)}
