"""Guardrails applied to every student-facing AI exchange, whichever model produced it."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Kept deliberately short and plain. Syllabus vocabulary (reproduction, puberty, hormones...)
# is never blocked: Chapters 9 and 10 are part of the Grade 8 syllabus.
_BLOCK = [r"\bf+u+c+k", r"\bsh[i1]t\b", r"\bb[i1]tch", r"\bbastard", r"\basshole", r"\bporn", r"\bnude",
          r"\bsexy\b", r"\bkill (you|him|her|them)\b", r"\bbomb\b", r"\bgun\b", r"\bdrugs? to get high\b",
          r"\bidiot\b", r"\bstupid\b", r"\bhate you\b"]
_DISTRESS = [r"\bkill myself\b", r"\bsuicid", r"\bself[- ]?harm", r"\bhurt myself\b", r"\bwant to die\b",
             r"\bend my life\b", r"\bbeing bullied\b", r"\bsomeone is hurting me\b"]
_JAILBREAK = [r"ignore (all |your |the )?(previous |above )?instructions", r"\bjust (tell|give) me the answer\b",
              r"what('?s| is) the (correct |right )?answer", r"\bsystem prompt\b", r"\bpretend (you are|to be)\b",
              r"\bdeveloper mode\b"]
_BLOCK_RE = re.compile("|".join(_BLOCK), re.I)
_DISTRESS_RE = re.compile("|".join(_DISTRESS), re.I)
_JAILBREAK_RE = re.compile("|".join(_JAILBREAK), re.I)

DISTRESS_REPLY = ("It sounds like something difficult is going on. You don't have to handle it alone. "
                  "Please talk to your class teacher, a parent or another adult you trust today. "
                  "If you are in danger right now, call 112.")
BLOCK_REPLY = "Let's keep our words kind and stay on the topic. What would you like to know about it?"
ANSWER_REQUEST_REPLY = ("I can't give the answer during the assignment, but I can help you work it out. "
                        "Let's take it one step at a time.")


@dataclass(frozen=True, slots=True)
class InputCheck:
    """Result of screening a student's message."""

    ok: bool
    reason: str
    reply: str | None = None


def screen_input(text: str) -> InputCheck:
    """Screen a student's message for distress, unkind or unsafe content, and answer-fishing."""
    if _DISTRESS_RE.search(text):
        return InputCheck(False, "distress", DISTRESS_REPLY)
    if _BLOCK_RE.search(text):
        return InputCheck(False, "blocked", BLOCK_REPLY)
    if _JAILBREAK_RE.search(text):
        return InputCheck(False, "answer_request", ANSWER_REQUEST_REPLY)
    return InputCheck(True, "ok")


def screen_output(text: str) -> bool:
    """Return True if an AI reply is safe to show a Grade 8 student."""
    return not (_BLOCK_RE.search(text) or len(text) > 1200)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def leaks_answer(reply: str, answers: list[str]) -> bool:
    """True if a hint reveals a correct answer: states it outright, or contains most of its words."""
    low = reply.lower()
    if re.search(r"\b(the )?(correct |right )?answer is\b|\boption [a-d] is (correct|right)\b", low):
        return True
    reply_tokens = set(_tokens(reply))
    for ans in answers:
        toks = [t for t in _tokens(ans) if len(t) > 2]
        if not toks:
            continue
        if len(toks) <= 3 and " ".join(toks) in " ".join(_tokens(reply)):
            return True
        if len(toks) > 3 and sum(t in reply_tokens for t in toks) / len(toks) >= 0.7:
            return True
    return False


def repeats_error(reply: str, wrong_signals: list[str]) -> bool:
    """True if the AI student's reply restates a wrong idea as fact (TA-4)."""
    low = reply.lower()
    return any(sig.lower() in low for sig in wrong_signals)


def _questions(text: str) -> set[str]:
    return {" ".join(re.findall(r"[a-z']+", q.lower())) for q in re.findall(r"[^.!?]*\?", text) if q.strip()}


def same_question(reply: str, previous: str) -> bool:
    """True if the reply asks a question NOVA has just asked (so NOVA doesn't ask the same thing again and again)."""
    return bool(_questions(reply) & _questions(previous))


_PLAIN = {"that", "this", "they", "them", "their", "there", "what", "which", "with", "from", "have", "think", "right",
          "like", "just", "really", "about", "into", "your", "yours", "does", "when", "then", "than", "also", "some"}


def restates_belief(reply: str, belief: str) -> bool:
    """True if a reply that hasn't been told it's right brings back most of NOVA's old wrong idea ('So plants do get
    some water through their leaves…'): over half of the belief's words. The fallback reply is then used instead."""
    words = {w for w in re.findall(r"[a-z]{4,}", belief.lower()) if w not in _PLAIN}
    low = reply.lower()
    if len(words) < 3:  # 'but that's not the main way' still brought the idea back, so a 'not' doesn't excuse it
        return False
    return sum(1 for w in words if re.search(rf"\b{w}", low)) / len(words) >= 0.5


def keep_short(text: str, max_sentences: int = 3, max_chars: int = 320) -> str:
    """Trim a reply for Grade 8 readers: a few short sentences."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out = " ".join(parts[:max_sentences])
    return out if len(out) <= max_chars else out[: max_chars - 1].rsplit(" ", 1)[0] + "…"
