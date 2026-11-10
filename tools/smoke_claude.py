"""Check the live Claude API connection with your key: one call per task, each validated.

Usage: python -m tools.smoke_claude   (reads ANTHROPIC_API_KEY from .env or the environment)
Exit code 0 when every task got a valid Claude answer (no offline fallback)."""
from __future__ import annotations

import sys
import time

from novaquest.ai.claude import ClaudeEngine
from novaquest.ai.provider import AIUnavailable, ClaudeProvider, advice
from novaquest.ai.types import TopicCtx
from novaquest.core.config import load_settings

TOPIC = TopicCtx(
    id=1, code="8.5", title="Parts of the Cell", outcome="Identify the cell membrane, cytoplasm and nucleus and state their functions.",
    terms=("cell membrane", "cytoplasm", "nucleus"), chapter_no=8, chapter_title="Cell — Structure and Functions", ai_concept=None,
    passages=({"id": 1, "ref": "NCERT Science Class VIII, Ch 8 §8.5, p. 93",
               "body": "The basic components of a cell are cell membrane, cytoplasm and nucleus. The cytoplasm and nucleus are enclosed "
                       "within the cell membrane, also called the plasma membrane. The membrane separates cells from one another and "
                       "also the cell from the surrounding medium. The plasma membrane is porous and allows the movement of substances "
                       "or materials both inward and outward. Nucleus is an important component of the living cell. It acts as the "
                       "control centre of the activities of the cell."},),
    misconceptions=({"id": "M8-2", "topic": "8.5", "label": "Cell wall and cell membrane are the same",
                     "nova_says": "Cell wall and cell membrane are just two names for the same thing.",
                     "correct_idea": "The cell membrane surrounds every cell; only plant cells also have a cell wall.",
                     "key_ideas": [["membrane"], ["wall"], ["plant"], ["animal"]], "wrong_signals": ["same thing"],
                     "probe": "Which cells have a cell wall?", "example_prompt": "What does the membrane do?"},))


def network(base_url: str) -> None:
    """How long each of the API's addresses takes to accept a connection (IPv4 and IPv6), to spot a slow network path."""
    import socket
    from urllib.parse import urlsplit

    u = urlsplit(base_url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        print(f"NET  cannot resolve {host}: {exc}")
        return
    seen = set()
    for family, kind, proto, _, addr in infos:
        name = "IPv4" if family == socket.AF_INET else "IPv6"
        if name in seen:
            continue
        seen.add(name)
        s = socket.socket(family, kind, proto)
        s.settimeout(3)
        t = time.perf_counter()
        try:
            s.connect(addr)
            print(f"NET  {name} {addr[0]:40} connected in {1000 * (time.perf_counter() - t):.0f} ms")
        except OSError:
            print(f"NET  {name} {addr[0]:40} no answer in 3 s (NovaQuest now skips it after 3 s and uses the next address)")
        finally:
            s.close()


def ping(provider: ClaudeProvider) -> bool:
    """One tiny call per model, so a key, credit or model-name problem is reported once, with what to do about it."""
    ok = True
    for tier, model in provider.models.items():
        t = time.perf_counter()
        try:
            provider.complete(tier=tier, system_stable="Reply with the word ready.", system_dynamic="",
                              messages=[{"role": "user", "content": "Ready?"}], max_tokens=16)
            print(f"OK   {tier:6} model {model:32} replied in {time.perf_counter() - t:4.1f} s"
                  + (" (sent without temperature)" if model in provider.no_temperature else ""))
        except AIUnavailable as exc:
            ok = False
            print(f"FAIL {tier:6} model {model:32} {exc}")
            tip = advice(exc.status, exc.detail)
            if tip:
                print(f"     What to do: {tip}")
    if not ok:
        print("Fix the problem above, then run this again. Until then NovaQuest keeps working with its offline engine.")
    return ok


def main() -> int:
    from tools.nq import load_dotenv

    load_dotenv()
    s = load_settings()
    if not s.anthropic_api_key:
        print("Set ANTHROPIC_API_KEY first.")
        return 2
    fallbacks: list[str] = []
    calls: list[tuple] = []

    def hook(tier, res, fallback):
        if fallback:
            fallbacks.append(tier)
        elif res:
            calls.append((tier, res.model, res.input_tokens, res.output_tokens, res.cache_read_tokens))
    provider = ClaudeProvider(s.anthropic_api_key, s.anthropic_base_url, s.model_fast, s.model_strong, s.ai_timeout_s)
    network(s.anthropic_base_url)
    if not ping(provider):
        return 1
    eng = ClaudeEngine(provider, hook)
    checks = []
    t = time.perf_counter()
    qs = eng.generate_questions([TOPIC], "mcq", "medium", 2)
    checks.append(("generate questions", bool(qs) and all(q.citation for q in qs), time.perf_counter() - t))
    t = time.perf_counter()
    hint = eng.tutor_hint({"kind": "mcq", "prompt": "Which part controls the cell?", "options": ["Nucleus", "Wall", "Vacuole", "Membrane"],
                           "answer": {"index": 0, "text": "Nucleus"}, "citation": []}, TOPIC, 1)
    checks.append(("tutor hint (no answer)", "nucleus" not in hint.text.lower(), time.perf_counter() - t))
    t = time.perf_counter()
    ans = eng.answer_question("What does the nucleus do?", [dict(TOPIC.passages[0])], TOPIC.title, True)
    checks.append(("grounded answer", bool(ans.citations), time.perf_counter() - t))
    t = time.perf_counter()
    mark = eng.mark_answer({"kind": "short", "prompt": "Name the parts of a cell.", "answer": {"model": "membrane, cytoplasm, nucleus"},
                            "rubric": [{"point": "membrane", "any": ["membrane"]}, {"point": "nucleus", "any": ["nucleus"]}]},
                           {"text": "The cell has a membrane outside and a nucleus that controls it."})
    checks.append(("marking", mark.score == 2.0, time.perf_counter() - t))
    t = time.perf_counter()
    verdict = eng.evaluate_teaching(TOPIC.misconceptions[0], "Yes, it's the same thing.", TOPIC.terms)
    checks.append(("Teach NOVA evaluator rejects a wrong idea", verdict.verdict == "incorrect", time.perf_counter() - t))
    for name, ok, secs in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {name:45} {secs:5.1f} s")
    for c in calls:
        print(f"     call: tier={c[0]} model={c[1]} in={c[2]} out={c[3]} cached={c[4]}")
    if fallbacks:
        print(f"FAIL offline fallback used for: {', '.join(fallbacks)}")
    return 0 if all(ok for _, ok, _ in checks) and not fallbacks else 1


if __name__ == "__main__":
    sys.exit(main())
