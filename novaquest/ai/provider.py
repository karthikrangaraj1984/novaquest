"""Model providers. Business code never calls a model API directly; it goes through LLMProvider."""
from __future__ import annotations

import http.client
import json
import logging
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("novaquest.ai")


class AIUnavailable(Exception):
    """The model could not be reached or returned an unusable reply. `status` is the HTTP status (if any) and `detail`
    the API's own reason (e.g. 'invalid_request_error: Your credit balance is too low...'); neither holds the key."""

    def __init__(self, message: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status, self.detail = status, detail


def api_error(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """(type, message) from a Claude API error body; empty strings if the body isn't the usual JSON."""
    try:
        body = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        return "", " ".join(err.split())[:300]
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("type", ""))[:60], " ".join(str(err.get("message", "")).split())[:300]


def advice(status: int | None, detail: str) -> str:
    """What to do about a Claude API error, in plain words."""
    d = detail.lower()
    if "credit balance" in d or "billing" in d:
        return "Your Anthropic account has no API credit. Add credit at console.anthropic.com (Settings > Billing)."
    if status == 401 or "x-api-key" in d or "authentication" in d:
        return "The API key was not accepted. Create a new key at console.anthropic.com and put it in .env."
    if status == 403 or "permission" in d:
        return "This key's organisation can't use that model. Check the key's workspace at console.anthropic.com."
    if status == 404 or "model" in d and ("not found" in d or "invalid" in d or "does not exist" in d):
        return "The model name isn't available to this key. Check NQ_MODEL_FAST and NQ_MODEL_STRONG in .env."
    if status == 429:
        return "Rate limit reached. Wait a minute, or raise the limit at console.anthropic.com."
    if status == 400:
        return "Claude refused the request; the reason above says why."
    return ""


CONNECT_TIMEOUT_S = 3.0  # per address: a network that can't reach one address (often IPv6) must not stall a call
_new_socket = socket.socket  # one place to make sockets (tests stand in a socket that never answers)


def fast_connect(address: tuple[str, int], timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address: tuple | None = None) -> socket.socket:
    """Like socket.create_connection, but tries IPv4 addresses first and gives each address at most a few seconds to
    connect. The standard version waits the whole request timeout on an address that never answers (common with IPv6
    on Windows networks), which made every Claude call take 20+ seconds before it fell back to the next address."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)  # stable: keeps the resolver's order within a family
    total = timeout if isinstance(timeout, (int, float)) else None
    per = min(CONNECT_TIMEOUT_S, total) if total else CONNECT_TIMEOUT_S
    last: OSError | None = None
    for family, kind, proto, _, addr in infos:
        sock = _new_socket(family, kind, proto)
        try:
            sock.settimeout(per)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            sock.settimeout(total)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last or OSError(f"could not resolve {host}")


class _FastHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = fast_connect


class _FastHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = fast_connect


class _FastHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_FastHTTPSConnection, req, context=self._context)


class _FastHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_FastHTTPConnection, req)


_OPENER = urllib.request.build_opener(_FastHTTPSHandler(), _FastHTTPHandler())


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Text and token usage from one model call."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    stop_reason: str = ""  # 'max_tokens' when the reply was cut off at the length limit


class LLMProvider(Protocol):
    """Anything that can complete a prompt for a tier ('fast' or 'strong')."""

    def complete(self, *, tier: str, system_stable: str, system_dynamic: str, messages: list[dict],
                 max_tokens: int, temperature: float = 0.3) -> LLMResult: ...


_NO_TEMPERATURE: set[str] = set()  # shared by every provider in the process, so each new batch remembers
_NO_TEMPERATURE_LOCK = threading.Lock()


class ClaudeProvider:
    """Claude Messages API over HTTPS with timeouts, retries and prompt caching."""

    def __init__(self, api_key: str, base_url: str, model_fast: str, model_strong: str,
                 timeout_s: float = 20.0, max_retries: int = 2) -> None:
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/v1/messages"
        self.models = {"fast": model_fast, "strong": model_strong}
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.no_temperature = _NO_TEMPERATURE  # models that refused the temperature setting (remembered process-wide)

    def _payload(self, tier: str, system_stable: str, system_dynamic: str, messages: list[dict],
                 max_tokens: int, temperature: float) -> bytes:
        system = [{"type": "text", "text": system_stable, "cache_control": {"type": "ephemeral"}}]
        if system_dynamic:  # long material (a chapter's text) is cached too: it's often sent several times in a row
            system.append({"type": "text", "text": system_dynamic, **({"cache_control": {"type": "ephemeral"}}
                                                                     if len(system_dynamic) > 8000 else {})})
        body = {"model": self.models[tier], "max_tokens": max_tokens, "system": system, "messages": messages}
        if self.models[tier] not in self.no_temperature:
            body["temperature"] = temperature
        return json.dumps(body).encode()

    LONG = 1000  # replies longer than this (questions, outlines, lesson drafts) are written in the background

    def timeout_for(self, max_tokens: int) -> float:
        """Seconds to wait for a reply. Short replies (hints, answers, marking) keep the short limit so a student never
        waits long; a long reply gets time for its length (a 6,000-token lesson draft can take a minute or more)."""
        return self.timeout_s if max_tokens <= self.LONG else max(self.timeout_s, 15 + max_tokens / 40)

    def complete(self, *, tier: str, system_stable: str, system_dynamic: str, messages: list[dict],
                 max_tokens: int, temperature: float = 0.3) -> LLMResult:
        """Call the model; retry transient failures (429, 5xx, timeouts) with backoff and jitter. A long reply honours
        the API's Retry-After when it is busy, and retries a timed-out call only once."""
        body = self._payload(tier, system_stable, system_dynamic, messages, max_tokens, temperature)
        headers = {"content-type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        long = max_tokens > self.LONG
        timeout = self.timeout_for(max_tokens)
        tries = self.max_retries + (2 if long else 0)
        timeouts = 0
        resent = False
        last: Exception | None = None
        attempt = -1
        while attempt < tries:
            attempt += 1
            sent_temperature = b'"temperature"' in body
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            wait = min(4.0, 0.5 * 2**attempt) + random.uniform(0, 0.3)
            try:
                with _OPENER.open(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                usage = data.get("usage", {})
                return LLMResult(text, data.get("model", self.models[tier]), int(usage.get("input_tokens", 0)),
                                 int(usage.get("output_tokens", 0)), int(usage.get("cache_read_input_tokens", 0) or 0),
                                 str(data.get("stop_reason") or ""))
            except urllib.error.HTTPError as exc:
                last = exc
                if long and exc.code in (429, 529):
                    try:  # busy or rate-limited: wait as long as the API asks (background work can wait)
                        wait = max(wait, min(60.0, float(exc.headers.get("retry-after") or 0) or 10.0 * (attempt + 1)))
                    except (TypeError, ValueError):
                        wait = max(wait, 10.0 * (attempt + 1))
                if exc.code not in (408, 429, 500, 502, 503, 504, 529):
                    kind, msg = api_error(exc)
                    model = self.models[tier]
                    if (exc.code == 400 and sent_temperature and not resent
                            and ("temperature" in msg.lower() or "top_p" in msg.lower())):
                        # Newer models set sampling themselves. Judged on what THIS request sent, so several calls in
                        # flight together each resend (before, only the first did and the others failed).
                        with _NO_TEMPERATURE_LOCK:
                            first = model not in self.no_temperature
                            self.no_temperature.add(model)
                        if first:
                            log.warning("model %s refused the temperature setting; sending without it", model)
                        body = self._payload(tier, system_stable, system_dynamic, messages, max_tokens, temperature)
                        resent = True
                        attempt -= 1  # the resend doesn't use up one of the retries
                        continue
                    detail = f"{kind}: {msg}" if kind and msg else (msg or kind)
                    raise AIUnavailable(f"model error {exc.code}" + (f" ({detail})" if detail else ""),
                                        status=exc.code, detail=detail) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                timeouts += isinstance(exc, TimeoutError) or "timed out" in str(exc)
                if long and timeouts >= 2:
                    break  # a long reply that timed out twice won't come; don't spend minutes more
            if attempt < tries:
                time.sleep(wait)
        raise AIUnavailable(f"model unreachable: {last}")


def salvage_list(text: str, key: str) -> list[dict]:
    """The complete objects of a JSON list in a reply that was cut off: from '{"questions": [{…}, {…}, {…' the first
    two. Empty if there are none."""
    at = text.find(f'"{key}"')
    start = text.find("[", at) if at >= 0 else -1
    if start < 0:
        return []
    dec, i, out = json.JSONDecoder(), start + 1, []
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] != "{":
            return out
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            return out
        if isinstance(obj, dict):
            out.append(obj)


def parse_json(text: str) -> dict:
    """The first complete JSON object in a model reply. Text after it (a note, or a second object) is ignored; before,
    reading to the last '}' in the reply turned such a reply into 'bad JSON: Extra data'."""
    start = text.find("{")
    if start < 0:
        raise AIUnavailable("reply had no JSON object")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise AIUnavailable(f"bad JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise AIUnavailable("reply had no JSON object")
    return obj
