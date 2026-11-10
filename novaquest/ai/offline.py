"""Deterministic AI engine built from the textbook passages and the reviewed syllabus map.

Used for tests, for demos without internet, and as the fallback when the Claude API fails.
It never invents facts: every sentence it shows comes from the textbook or the reviewed map."""
from __future__ import annotations

import random
import re
from itertools import zip_longest
from typing import Any, Sequence

from novaquest.ai import guard
from novaquest.ai.types import Mark, QuestionDraft, TeachVerdict, TopicCtx, TutorReply

THINKING = {"easy": "remember", "medium": "apply", "hard": "analyse"}
_EXAMPLE_MARKERS = ("for example", "example", "e.g", "such as", "like a", "like the", "for instance", "imagine")
_CORRECTING = re.compile(r"\b(but|not|no|only|actually|except|instead|however|isn'?t|aren'?t|doesn'?t|don'?t)\b", re.I)
_SURE = re.compile(r"^\s*((yes|yeah|yep)[\s,!.]*)?(you are right|you'?re right|that'?s right|i agree|exactly|correct|true)\b", re.I)
_AGREE = re.compile(r"^\s*(yes|yeah|yep|right|correct|true|you are right|you're right|exactly)\b", re.I)
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "it", "that", "this", "on", "for", "with",
         "as", "by", "be", "can", "do", "does", "what", "why", "how", "which", "from", "at", "we", "you", "i"}


def _stem(tok: str) -> str:
    """A light plural/tense stemmer: viruses→virus (not virus→viru), bacteria stays, cells→cell, boxes→box."""
    if len(tok) <= 4:
        return tok
    if tok.endswith("ies"):
        return tok[:-3] + "y"
    if tok.endswith(("sses", "shes", "ches", "xes", "zes", "uses")):
        return tok[:-2]
    if tok.endswith(("us", "ss", "is")):
        return tok
    for suf in ("ing", "ed", "s"):
        if tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _toks(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower())]


def phrase_in(phrase: str, text_toks: Sequence[str]) -> bool:
    """True if every word of `phrase` appears in the tokenised text."""
    ptoks = _toks(phrase)
    if not ptoks:
        return False
    if len(ptoks) == 1:
        return ptoks[0] in text_toks
    joined = " " + " ".join(text_toks) + " "
    return (" " + " ".join(ptoks) + " ") in joined or all(t in text_toks for t in ptoks)


_NEGATION = re.compile(r"\b(not|no|never|isn'?t|aren'?t|doesn'?t|don'?t|can'?t|cannot|wrong|false|myth)\b[\w\s,']{0,20}$", re.I)


def asserts(text: str, phrase: str) -> bool:
    """True if `text` states `phrase` as true: present, and not negated just before it ('not just water')."""
    low, p = text.lower(), phrase.lower()
    start = low.find(p)
    while start >= 0:
        if not _NEGATION.search(low[max(0, start - 30):start]):
            return True
        start = low.find(p, start + 1)
    return False


_LINKS = {"is", "are", "was", "were", "be", "has", "have", "had", "because", "so", "which", "that", "when", "if", "then",
          "means", "called", "makes", "make", "helps", "help", "can", "cannot", "do", "does", "will", "gives", "give",
          "causes", "cause", "used", "uses", "turns", "changes", "keeps", "stops", "lets", "allows", "needs", "need",
          "contains", "like", "such", "example", "while", "but", "and", "into", "by", "from", "of", "in", "to", "with"}


def explains(text: str) -> bool:
    """True if an answer reads like an explanation: real sentences with linking words, not a bare list."""
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 5:
        return False
    links = sum(1 for w in words if w in _LINKS)
    return links >= 2 and links / len(words) >= 0.12


def keyword_list(text: str) -> bool:
    """True if an answer is just key words (a list or a few words), which shows recall at most, not understanding."""
    return not explains(text)


def _glued_heading(sentence: str) -> bool:
    """True if a section heading was glued onto the sentence by the PDF ('Preservation by Sugar Jams, jellies…',
    'Chemical Method Salts and edible oils…'): several capitalised words at the start, before any small word runs."""
    words = sentence.split()[:6]
    caps = [w for w in words[1:5] if w[:1].isupper() and not w.isupper()]
    if len(caps) >= 2 and not re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+ (discovered|found|invented|showed|said|observed)\b", sentence):
        return True
    if re.search(r"(^|\s)\d{1,2}\.\d{1,2}\s+[A-Z]", sentence):  # a numbered section heading ('8.1 Discovery of…')
        return True
    # a figure label glued onto the sentence's own first word: 'Nucleoid Some microorganisms, such as bacteria…'
    if re.match(r"^[A-Z][a-z]+ (Some|Most|Many|All|The|These|This|Those|It|Its|Each|Every|Such|Our|We|They|There)\s", sentence) \
            and sentence.split()[0] not in ("In", "At", "On", "For", "Of", "By", "From", "With", "As", "Like", "Unlike", "When", "If"):
        return True
    # a heading or question glued into the middle: '…artificial setting As children move ahead…'
    return bool(re.search(r"[a-z] (As|The|This|These|It|We|You|They|When|If|Now|So|Then|But|Why|How|What|Can)\s[a-z]", sentence))


_NARRATION = re.compile(r"\b(explains? that|tells? (?:them|him|her|us)|told (?:them|him|her)|asks?|asked|says|said|"
                        r"wonders?|wondered|recall that|let us|you will|you have|you might|you can|you must|"
                        r"I want|I wonder|I think|I am|I have|we will|we shall|you shall|in this chapter|"
                        r"try to|record your|let us|activity)\b", re.I)


# Sentences that lean on something the student can't see: an earlier class or chapter ('as you learnt in Class VII').
_ELSEWHERE = re.compile(r"\bClass(es)?\s+(VI|VII|VIII|IX|X|[6-9]|10)\b|\byou (have |also |may have |might have )?(learnt|learned|studied|read|saw|seen)\b|"
                        r"\bas (we|you) (know|learnt|learned|saw)\b|\b(earlier|previous|last) (class|classes|chapter|year)\b|"
                        r"\bChapter\s+\d+\b", re.I)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z‘“(])", text)
    return [p.strip() for p in parts if 8 <= len(p.split()) <= 38 and p.strip()[0].isupper() and p.strip().endswith(".")
            and not re.search(r"\b(Figs?|Figures?|Tables?|Activit(?:y|ies)|Snapshots?)\b|Boojho|Paheli|\(\w\)|[A-Z]{4,}", p) and "?" not in p
            and not _ELSEWHERE.search(p) and not _glued_heading(p.strip()) and not re.search(r"\.\s+[a-z]", p)]


def _facts(text: str) -> list[str]:
    """Textbook sentences that state a fact on their own: no story lines, instructions, or sentences that lean on
    the one before (used for marking points and model answers)."""
    return [x for x in _sentences(text) if not _NARRATION.search(x) and not _NOT_A_FACT.match(x) and not _LEANS.match(x)]


def _leaks(sentence: str, term: str) -> bool:
    """True if the rest of the sentence gives the blanked word away: a part of it or a word built on it
    ('_____ (pseudo: false; podia: feet)' for pseudopodia, 'germinate … _____' for germination)."""
    t = re.sub(r"[^a-z]", "", term.lower())
    rest = re.sub(r"\b" + re.escape(term) + r"\b", " ", sentence, count=1, flags=re.I).lower()
    for w in re.findall(r"[a-z]{4,}", rest):
        if (len(w) >= 5 and w in t) or t in w or (len(w) >= 5 and len(t) >= 5 and _stem(w)[:5] == _stem(t)[:5]):
            return True
    return False


# Reasons, purposes and consequences stated in the textbook: the basis of 'why' and 'what happens if' questions.
_REASON = [
    ("purpose", re.compile(r"^(?P<a>[A-Z][^;:]{15,130}?)\s+(?P<b>(?:in order )?to (?:prevent|check|stop|avoid|protect|kill|remove|"
                           r"reduce|increase|improve|keep|make|get|save|control|produce|obtain|help|carry|supply|provide|"
                           r"absorb|separate|detect|measure|grow)\b[^.;]{5,90})\.$")),
    ("cause", re.compile(r"^(?P<a>[A-Z][^;:]{15,140}?),?\s+because\s+(?P<b>[^.;]{8,100})\.$")),
    ("result", re.compile(r"^If (?P<a>[^,]{8,100}),\s*(?P<b>[^.;]{8,110})\.$")),
]


def _clauses(texts, kind: str) -> list[tuple[str, str, str]]:
    """(sentence, a, b) for every textbook sentence of one reasoning kind."""
    pat = dict(_REASON)[kind]
    out = []
    for body in texts:
        for sent in _sentences(body):
            m = pat.match(sent)
            if m and not re.search(r"\b(I|you|we)\b", m.group("b")) and not _glued_heading(m.group("b")) \
                    and not _NOT_A_FACT.match(m.group("b")) and not _NOT_A_FACT.match(m.group("a")):
                out.append((sent, m.group("a").strip(), m.group("b").strip()))
    return out


def _vague(reason: str) -> bool:
    """A reason too vague to be a believable wrong option: 'to get sick', 'to improve this understanding'."""
    r = reason.lower().strip()
    return bool(re.search(r"\bthis (understanding|idea|way|thing|fact)\b", r)) or (r.startswith("to ") and len(_content_words(r)) < 2)


_QUESTION_TITLE = re.compile(r"^(how|what|why|when|where|which|who|do|does|did|is|are|can|could|should|will)\b", re.I)


def _about(title: str) -> str:
    """A topic title as it reads inside a question: 'Do Liquids Conduct Electricity?' -> 'do liquids conduct electricity'."""
    return title.strip().rstrip("?").strip().lower()


def _which(title: str, negative: bool = False) -> str:
    """'Which statement about … is correct?' for a topic; a topic titled as a question is asked first, so it never
    reads 'Which statement about how does a battery generate electricity is correct?'."""
    t = " ".join(title.split()).strip()
    tail = "NOT correct" if negative else "correct"
    if t.endswith("?") or _QUESTION_TITLE.match(t):
        return f"{t.rstrip('?').strip()}? Which statement is {tail}?"
    return f"Which statement about {_about(t)} is {tail}?"


def _articles(text: str) -> str:
    """Fix 'a'/'an' after a word was swapped in ('a embryo' -> 'an embryo', 'an lightning conductor' -> 'a lightning …')."""
    def fix(m: re.Match) -> str:
        art, word = m.group(1), m.group(2)
        want = "an" if word[:1].lower() in "aeiou" and not re.match(r"(?i)u[nr]?i|eu|one", word) else "a"
        return (want.capitalize() if art[:1].isupper() else want) + " " + word
    # lower-case 'a'/'an' anywhere; capital 'A'/'An' only at the start ('vitamin A in food' has no article)
    return re.sub(r"(^(?:A|An)|\b(?:a|an))\s+([A-Za-z]\w*)", fix, text)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


_GENERIC = {"compound", "substance", "material", "chemical", "organism", "thing", "element", "product", "matter",
            "living", "object", "item", "part", "type", "kind", "process", "method", "animal", "plant", "food"}
# Everyday describing words ('shape', 'size', 'number'): swapping one for a science term gives nonsense
# ('organs vary in cytoplasm, size and number'), and blanking one only tests English.
_ATTRIBUTE = {"shape", "shapes", "size", "sizes", "number", "numbers", "function", "functions", "form", "forms", "colour",
              "color", "structure", "structures", "length", "weight", "mass", "volume", "area", "level", "amount", "rate",
              "speed", "position", "direction", "quantity", "nature", "role", "use", "uses", "work", "growth", "change",
              "changes", "variety", "group", "groups", "feature", "features", "property", "properties", "way", "ways",
              "example", "examples", "body", "life", "place", "time", "cell", "cells"}


_ADJ = {"cheap", "clean", "hard", "soft", "light", "heavy", "good", "bad", "hot", "cold", "dry", "wet", "small", "large",
        "big", "high", "low", "strong", "weak", "fresh", "pure", "safe", "simple", "basic", "acidic", "neutral", "useful",
        "harmful", "toxic", "poor", "rich", "slow", "fast", "loud", "dull", "shiny", "bright", "dark", "thin", "thick"}


_ANT_NOUNS = {"pollutant", "pollutants", "plant", "plants", "giant", "elephant", "elephants", "ant", "ants", "fertilisant",
              "lubricant", "lubricants", "reactant", "reactants", "coolant", "disinfectant", "antioxidant", "deodorant", "propellant",
              "sealant", "infant", "infants", "migrant", "migrants", "servant", "assistant", "inhabitant", "inhabitants"}


def _word_kind(term: str) -> str:
    """A rough part of speech from the last word ('noun' unless it looks like an adjective or a verb), so a swapped-in
    word reads like the one it replaces ('water' is never swapped for 'cheap' or 'vaporise')."""
    w = term.lower().split()[-1] if term.split() else term.lower()
    if w in _ADJ or re.search(r"(able|ible|ous|ive|ful|less|ic|ent|ary|ish|pleasant|ant)$", w) and w not in _ANT_NOUNS:
        return "adj"
    if w in _VERBS or re.search(r"(ise|ize|ify|ing)$", w):
        return "verb"
    return "noun"


_VERBS = {"recycle", "reuse", "reduce", "deform", "conduct", "burn", "melt", "dissolve", "absorb", "reflect", "refract",
          "evaporate", "condense", "germinate", "sow", "plough", "harvest", "store", "grow", "breathe", "digest"}
# Lab steps and sentences that lean on the one before ('Hence, …', 'This is …') aren't facts a student can judge alone.
_NOT_A_FACT = re.compile(r"^(If it does,\s*)?(Note|Draw|Take|Fix|Wash|Observe|Put|Place|Look|Try|Repeat|Keep|Mark|Make|Cut|"
                         r"Write|Fill|Add|Pour|Hold|Bring|Collect|Measure|Do|Let|Find|See|Use|Ask|Get|Hence|Thus|So|This|"
                         r"That|These|Those|It|They|Such|Therefore|Also|However|But|And|Then)\b", re.I)
# Whole sentences that lean on the one before or talk to the reader: never a stand-alone fact to blank or swap.
_LEANS = re.compile(r"^(Your|Our|As a result|As you|As we|In fact|In both|In this|In that|In such|From these|From this|"
                    r"For this|Here|Recall|Remember|Now|Similarly|Likewise|Instead|Moreover|Besides)\b", re.I)


def _content_words(text: str) -> set[str]:
    """The meaningful words of a phrase, stemmed (for 'do these two say the same thing?' checks)."""
    return {_stem(w) for w in re.findall(r"[a-z]{4,}", text.lower())} - {"that", "this", "with", "from", "they", "their",
                                                                         "them", "have", "into", "also", "such", "some"}


def _is_name(term: str) -> bool:
    """A proper name ('Satpura', 'Lactobacillus'), not an abbreviation ('CNG') or an ordinary word."""
    first = term.split()[0] if term.split() else term
    return first[:1].isupper() and len(first) > 1 and not first.isupper()  # 'Y chromosome', 'CNG' are not names


def _base(term: str) -> str:
    return " ".join(_stem(w) for w in re.findall(r"[a-z0-9]+", term.lower()))


def _distractors(term: str, pool, sentence: str, rng: random.Random, k: int = 3, strict: bool = False) -> list[str]:
    """Believable wrong options: the same kind of word as the answer (same number of words, singular or plural, a
    similar ending and length), never a near-copy of the answer ('weeds'/'weeding') and never a word already in the
    sentence."""
    t = term.lower()
    ending = re.search(r"(tion|sion|ing|ity|ism|al|er|or|ure|ide|ate|ogy|ant|ive)$", t)
    scored = []
    seen = set()
    for x in pool:
        xl = x.lower()
        if xl in seen or xl == t or re.search(r"\b" + re.escape(x) + r"\b", sentence, re.I):
            continue
        if _is_name(x) != _is_name(term) or (strict and not x[:1].isalpha()):
            continue
        if bool(re.fullmatch(r"[\d.,]+", x.strip())) != bool(re.fullmatch(r"[\d.,]+", term.strip())):
            continue  # a year or number is never a wrong option for a word, or the other way round  # a name ('Satpura') is never a wrong option for an ordinary word, or the other way round
        seen.add(xl)
        bx, bt = _base(x), _base(term)
        if bx == bt or bx.startswith(bt) or bt.startswith(bx) or set(bx.split()) & set(bt.split()):
            continue  # a near-copy of the answer would make two right options
        score = 3 * (len(x.split()) == len(term.split())) + (xl.endswith("s") == t.endswith("s")) \
            + (abs(len(x) - len(term)) <= 4) + 2 * bool(ending and xl.endswith(ending.group(1))) \
            + 3 * (_word_kind(x) == _word_kind(term)) - 3 * (xl in _ATTRIBUTE)
        if strict and (score < 7 or set(_base(x).split()) & _GENERIC or x.lower() in _ATTRIBUTE or _word_kind(x) != _word_kind(term)
                       or not re.fullmatch(r"[A-Za-z][A-Za-z \-]*", x)):
            continue  # swapped into a sentence: the same kind of word, and never a general word that could still be true
        scored.append((score, rng.random(), x))
    scored.sort(reverse=True)
    picked = [x for _, _, x in scored[:k]]
    if len(picked) < k and not strict:  # few key words in this chapter: allow words that only share a part with the answer
        for x in pool:
            if len(picked) >= k:
                break
            if bool(re.fullmatch(r"[\d.,]+", x.strip())) != bool(re.fullmatch(r"[\d.,]+", term.strip())):
                continue
            if x.lower() != t and x not in picked and _is_name(x) == _is_name(term) and _base(x) != _base(term) and not (_base(x).startswith(_base(term))
                    or _base(term).startswith(_base(x))) and not re.search(r"\b" + re.escape(x) + r"\b", sentence, re.I):
                picked.append(x)
    return picked


def _mask(sentence: str, term: str) -> str | None:
    pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
    if not pattern.search(sentence):
        return None
    return pattern.sub("_____", sentence, count=1)


def _key(q: QuestionDraft) -> str:
    """What makes two questions the same: the prompt plus the answer's text (not its position)."""
    return str((q.answer or {}).get("text") or (q.answer or {}).get("model") or q.misconception_codes)


def _cite(p: dict, quote: str) -> dict:
    return {"passage_id": p["id"], "ref": p["ref"], "quote": quote[:220]}


def _is_maths(course: str | None) -> bool:
    return bool(re.search(r"\bmath|ganit", course or "", re.I))


class OfflineEngine:
    """Rule-based engine grounded in the stored syllabus."""

    name = "offline"

    # ---------- question generation ----------
    def generate_questions(self, topics: Sequence[TopicCtx], kind: str, difficulty: str, count: int,
                           seed: int = 0) -> list[QuestionDraft]:
        """Draft `count` questions of one kind spread across the topics."""
        rng = random.Random(seed)
        makers = {"mcq": self._mcq, "short": self._short, "viva": self._viva, "ai_moment": self._ai_moment,
                  "teach": self._teach, "learn": self._learn}
        if kind == "mission":
            return self._mission(topics, difficulty, count, rng)
        if kind == "numeric":
            return self._numeric(topics, difficulty, count, seed)
        if kind in ("mcq", "short", "viva", "mission") and topics and _is_maths(topics[0].course):
            got = self._maths(topics, kind, difficulty, count, seed)
            if got:  # a maths book: worked problems, not sentences from the book
                return got
        out: list[QuestionDraft] = []
        attempts = 0
        while len(out) < count and attempts < count * 12:
            topic = topics[attempts % len(topics)]
            q = makers[kind](topic, difficulty, rng, attempts // len(topics))
            attempts += 1
            if q and all((q.prompt, _key(q)) != (o.prompt, _key(o)) for o in out):
                out.append(q)
        return out

    def _maths(self, topics: Sequence[TopicCtx], kind: str, difficulty: str, count: int, seed: int) -> list[QuestionDraft]:
        """Maths questions without Claude (1.24.0): multiple choice whose wrong options are the usual slips, and
        'show your working' questions marked on the method's steps, from the ready-made problems that fit each topic."""
        from novaquest.services import maths

        lists: list[list[QuestionDraft]] = []
        per = max(2, -(-count // len(topics)))
        for t in topics:
            out: list[QuestionDraft] = []
            lists.append(out)
            name = " ".join([t.title, t.chapter_title, " ".join(t.terms)])
            text = name + " " + " ".join(p["body"][:1500] for p in t.passages[:4])
            if kind in ("short", "viva"):
                for prompt, key, level in maths.from_library(name, per, seed + t.id) or maths.from_library(text, per, seed + t.id):
                    final = maths.fmt(float(key["value"]), key.get("decimals"))
                    rubric = [{"point": st, "any": [n for n in re.findall(r"\d+(?:\.\d+)?", st)][:3] or [final]}
                              for st in key.get("steps", [])][:3]
                    rubric.append({"point": f"Correct answer: {maths.show(float(key['value']), key.get('unit') or '', key.get('decimals'))}",
                                   "any": [final]})
                    if kind == "viva":  # a long answer in two parts: the method in words, then the working
                        rubric[:1] = [{"point": "Explains the method: " + (key.get("steps") or ["the right rule"])[0],
                                          "any": [w for w in re.findall(r"[a-z]{5,}", " ".join(key.get("steps") or []).lower())][:4]
                                          or [final]}]
                        q = (f"Part 1 (explain): {prompt} Which rule or method do you need to solve this, and why? "
                             f"Part 2 (apply): Now solve it, showing your working.")  # the problem comes first, in (a)
                    else:
                        q = f"{prompt} Show your working."
                    out.append(QuestionDraft(topic_id=t.id, kind=kind, prompt=q, difficulty=difficulty,
                                             thinking_level="analyse" if kind == "viva" else level, outcome=t.outcome,
                                             rubric=rubric, answer={"model": key.get("worked") or final}))
            else:
                for q in maths.mcq_from_library(name, per, seed + t.id) or maths.mcq_from_library(text, per, seed + t.id):
                    out.append(QuestionDraft(topic_id=t.id, kind="mcq", prompt=q["prompt"], difficulty=difficulty,
                                             thinking_level=q["level"], outcome=t.outcome, options=q["options"],
                                             answer={"index": q["answer"], "text": q["options"][q["answer"]], "why": q["why"]}))
        mixed = [d for row in zip_longest(*lists) for d in row if d]  # round the topics, so every topic gets a turn
        seen: set[str] = set()
        uniq = [d for d in mixed if not (d.prompt in seen or seen.add(d.prompt))]
        return uniq[:count]

    def _numeric(self, topics: Sequence[TopicCtx], difficulty: str, count: int, seed: int) -> list[QuestionDraft]:
        """Number problems from NovaQuest's ready-made set that fit the topics' words (area, speed, pressure…)."""
        from novaquest.services import maths

        lists: list[list[QuestionDraft]] = []
        per = max(2, -(-count // len(topics)))
        for t in topics:
            name = " ".join([t.title, t.chapter_title, " ".join(t.terms)])  # the topic's own words first, then its text
            found = maths.from_library(name, per, seed + t.id) or maths.from_library(
                name + " " + " ".join(p["body"][:1500] for p in t.passages[:4]), per, seed + t.id)
            lists.append([QuestionDraft(topic_id=t.id, kind="numeric", prompt=prompt, difficulty=difficulty,
                                        thinking_level=level, outcome=t.outcome, answer=key) for prompt, key, level in found])
        out: list[QuestionDraft] = []
        for d in (d for row in zip_longest(*lists) for d in row if d):  # round the topics, so every topic gets a turn
            if len(out) < count and d.prompt not in {o.prompt for o in out}:
                out.append(d)
        return out

    def _mcq(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        """Easy: recall (fill in the blank). Medium and hard: mostly 'why' and 'what happens if' questions built on
        the textbook's own reasons, and correcting a common mix-up; recall only fills gaps."""
        own = [m for m in t.misconceptions if m["topic"] == t.code]
        if difficulty == "easy":
            return self._cloze(t, difficulty, rng, n)
        tries = [lambda: self._reason_mcq(t, difficulty, rng, n),
                 lambda: self._statement_mcq(t, difficulty, rng, n),
                 lambda: self._misconception_mcq(t, own[n % len(own)], difficulty, rng) if own else None]
        shift = n % len(tries)
        tries = tries[shift:] + tries[:shift]
        for make in tries:
            q = make()
            if q:
                return q
        return self._cloze(t, difficulty, rng, n)

    def _statement_mcq(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        """Medium: 'Which statement is correct?' (one textbook sentence, three with a key word swapped).
        Hard: 'Which statement is NOT correct?' (three textbook sentences, one with a key word swapped)."""
        facts = []
        for p in t.passages:
            for sent in _sentences(p["body"]):
                if len(sent) > 170 or _NARRATION.search(sent) or _NOT_A_FACT.match(sent) or _LEANS.match(sent):
                    continue  # stories, lab steps and sentences that need the one before aren't facts to test
                for x in t.terms:
                    if x.lower() in _ATTRIBUTE:
                        continue  # never swap 'shape' or 'size': the wrong statements would be nonsense, not believable
                    m = re.search(r"\b" + re.escape(x) + r"\b", sent, re.I)
                    # skip a term used inside a name ('Satpura Tiger Reserve'): capitalised mid-sentence
                    if m and not (m.start() > 0 and sent[m.start()].isupper() and not x[:1].isupper()):
                        facts.append((p, sent, x))
                        break
        if not facts:
            return None
        pool = list(t.terms) + list(t.sibling_terms)

        def swap(sent: str, term: str, k: int) -> list[str]:
            out = []
            for w in _distractors(term, pool, sent, rng, k, strict=True):
                new = re.sub(r"\b" + re.escape(term) + r"\b", w, sent, count=1, flags=re.I)
                new = _articles(new)
                out.append(_cap(new) if sent[:1].isupper() else new)
            return out

        if difficulty != "hard":
            p, sent, term = facts[n % len(facts)]
            false = swap(sent, term, 3)
            if len(false) < 3:
                return None
            right, options = sent, false + [sent]
            prompt = _which(t.title)
        else:
            if len(facts) < 4:
                return None
            start = n % len(facts)
            chosen = [facts[(start + i) % len(facts)] for i in range(4)]
            if len({c[1] for c in chosen}) < 4:
                return None
            p, sent, term = chosen[0]
            false = swap(sent, term, 1)
            if not false:
                return None
            right, options = false[0], [false[0]] + [c[1] for c in chosen[1:]]
            prompt = _which(t.title, negative=True)
        rng.shuffle(options)
        return QuestionDraft(topic_id=t.id, kind="mcq", prompt=prompt, difficulty=difficulty,
                             thinking_level="analyse" if difficulty == "hard" else "apply", outcome=t.outcome,
                             options=options, answer={"index": options.index(right), "text": right},
                             citation=[_cite(p, sent)])

    def _reason_mcq(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        """A 'why' (cause or purpose) or 'what happens if' question from a reason the textbook states; the wrong
        options are other reasons from the same chapter, so they all sound plausible."""
        found = [(kind, c) for kind, _ in _REASON for c in _clauses([p["body"] for p in t.passages], kind)]
        if not found:
            return None
        kind, (sentence, a, b) = found[n % len(found)]
        # Wrong reasons come only from OTHER topics, and never share a key word with this topic or the right reason:
        # within one topic every stated reason is a true one, so it would make two right answers.
        own_words = {w for x in [b] + list(t.terms) + [c[2] for _, c in found] for w in _content_words(x)}
        others = [c[2] for c in _clauses(t.sibling_text, kind)]
        others = [o for o in dict.fromkeys(others) if _base(o) != _base(b) and not (_content_words(o) & own_words)
                  and not _vague(o)]
        if len(others) < 3:
            return None
        rng.shuffle(others)
        if kind == "result":
            prompt, right, wrong = f"What happens if {a}?", _cap(b) + ".", [_cap(o) + "." for o in others[:3]]
        elif kind == "cause":
            prompt, right, wrong = f"Why? “{a}.”", f"Because {b}.", [f"Because {o}." for o in others[:3]]
        else:
            prompt, right, wrong = f"What is the reason? “{a} …”", _cap(b) + ".", [_cap(o) + "." for o in others[:3]]
        options = wrong + [right]
        rng.shuffle(options)
        src = next(p for p in t.passages if sentence in p["body"])
        return QuestionDraft(topic_id=t.id, kind="mcq", prompt=prompt, difficulty=difficulty,
                             thinking_level="analyse" if difficulty == "hard" else "apply", outcome=t.outcome,
                             options=options, answer={"index": options.index(right), "text": right},
                             citation=[_cite(src, sentence)])

    def _cloze(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        candidates = []
        for p in t.passages:
            for s in _sentences(p["body"]):
                for term in t.terms:
                    if term.lower() in _ATTRIBUTE:
                        continue
                    if _NARRATION.search(s) or _NOT_A_FACT.match(s) or _LEANS.match(s):
                        break  # stories ('Their aunt told them…') and sentences that need the one before
                    masked = _mask(s, term)
                    if masked and masked.count("_____") == 1 and not masked.startswith("_____") and not _leaks(s, term):
                        candidates.append((p, s, term, masked))
        if not candidates:
            return self._term_match(t, difficulty, rng) if difficulty == "easy" else None
        p, sentence, term, masked = candidates[(n * 7 + rng.randrange(len(candidates))) % len(candidates)]
        wrong = _distractors(term, list(t.terms) + list(t.sibling_terms), sentence, rng)
        if len(wrong) < 3:
            return None
        options = wrong + [term]
        rng.shuffle(options)
        return QuestionDraft(
            topic_id=t.id, kind="mcq", prompt=f"Fill in the blank: {masked}", difficulty=difficulty,
            thinking_level="remember", outcome=t.outcome, options=options,
            answer={"index": options.index(term), "text": term},
            citation=[_cite(p, sentence)])

    def _term_match(self, t: TopicCtx, difficulty: str, rng: random.Random) -> QuestionDraft | None:
        """Fallback when no clean textbook sentence exists: match the outcome to its key term."""
        if not t.terms:
            return None
        term = t.terms[0]
        wrong = _distractors(term, [x for x in t.sibling_terms if x.lower() not in {y.lower() for y in t.terms}], "", rng)
        if len(wrong) < 3:
            return None
        options = wrong + [term]
        rng.shuffle(options)
        cite = [_cite(t.passages[0], t.passages[0]["body"][:200])] if t.passages else []
        return QuestionDraft(topic_id=t.id, kind="mcq", prompt=f"Which key word belongs with this idea? “{t.outcome}”",
                             difficulty=difficulty, thinking_level="remember", outcome=t.outcome, options=options,
                             answer={"index": options.index(term), "text": term}, citation=cite)

    def _misconception_mcq(self, t: TopicCtx, m: dict, difficulty: str, rng: random.Random) -> QuestionDraft | None:
        right = m["correct_idea"].split(". ")[0].rstrip(".") + "."
        labels = [m["label"]] + [x["label"] for x in t.misconceptions if x["id"] != m["id"]] + list(t.other_wrong)
        labels = list(dict.fromkeys(labels))[:3]
        if difficulty == "hard":
            prompt = f"NOVA, an AI student, says: “{m['nova_says']}” Which reply best corrects NOVA?"
            wrong = [f"You're right: {lb[0].lower() + lb[1:]}." for lb in labels]
            for filler in ("You're right, NOVA. There is nothing to correct.",
                           "Nobody knows, so any answer is fine."):
                if len(wrong) < 3:
                    wrong.append(filler)
            level = "analyse"
        else:
            if len(labels) < 3:
                return None
            prompt = _which(t.title)
            wrong = [lb.rstrip(".") + "." for lb in labels]
            level = "apply"
        options = [right] + wrong
        rng.shuffle(options)
        cite = [_cite(t.passages[0], t.passages[0]["body"][:200])] if t.passages else []
        return QuestionDraft(topic_id=t.id, kind="mcq", prompt=prompt, difficulty=difficulty, thinking_level=level,
                             outcome=t.outcome, options=options, answer={"index": options.index(right), "text": right},
                             misconception_codes=[m["id"]], citation=cite)

    def _rubric_from_terms(self, t: TopicCtx, k: int = 3, skip: int = 0) -> list[dict]:
        """Marking points as ideas: each is a textbook sentence about a key term, credited by any of its key words
        (the term or the other important words of that sentence), so answers in the student's own words count."""
        body = " ".join(p["body"] for p in t.passages).lower()
        ranked = sorted(t.terms, key=lambda term: -body.count(term.lower()))
        if ranked and skip:
            ranked = ranked[skip % len(ranked):] + ranked[:skip % len(ranked)]
        sents = [x for p in t.passages for x in _facts(p["body"])]
        points, used = [], set()
        for term in ranked:
            sent = next((x for x in sents if x not in used and phrase_in(term, _toks(x))), None)
            if sent:
                used.add(sent)
                extra = [w for w in dict.fromkeys(re.findall(r"[a-z]{5,}", sent.lower()))
                         if w not in _STOP and w not in _LINKS and w != term.lower()][:3]
                others = [x for x in t.terms if x != term and phrase_in(x, _toks(sent))]
                idea = sent if len(sent) <= 110 else sent[:107].rsplit(" ", 1)[0] + "…"
                points.append({"point": f"Explains: {idea}", "any": [term] + others[:2] + extra})
            else:
                points.append({"point": f"Uses the idea of “{term}” correctly", "any": [term]})
            if len(points) == k:
                break
        return points

    def _model_answer(self, t: TopicCtx, rubric: list[dict]) -> tuple[str, list[dict]]:
        picked: list[str] = []
        cites: list[dict] = []
        for point in rubric:
            if point["point"].startswith("Explains: "):  # the idea's own sentence
                sent = next((x for p in t.passages for x in _facts(p["body"]) if x.startswith(point["point"][10:].rstrip("…")[:60])), None)
                src = next((p for p in t.passages if sent and sent in p["body"]), None)
                if sent and src and sent not in picked:
                    picked.append(sent)
                    cites.append(_cite(src, sent))
                    continue
            for p in t.passages:
                hit = next((s for s in _facts(p["body"]) if phrase_in(point["any"][0], _toks(s))), None)
                if hit and hit not in picked:
                    picked.append(hit)
                    cites.append(_cite(p, hit))
                    break
        if not picked:  # no clean textbook sentence: say what a good answer must cover, never echo the question
            ideas = [pt["any"][0] for pt in rubric if pt.get("any")]
            return "A good answer explains " + ", ".join(ideas) + ", using the textbook section on " + t.title.lower() + ".", cites
        return " ".join(picked), cites

    def _short(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft:
        own = [m for m in t.misconceptions if m["topic"] == t.code]
        if difficulty == "hard" and own:
            m = own[n % len(own)]
            rubric = [{"point": "Mentions: " + " / ".join(dict.fromkeys(grp[:3])), "any": grp} for grp in m["key_ideas"]]
            return QuestionDraft(topic_id=t.id, kind="short", prompt=m["probe"], difficulty=difficulty,
                                 thinking_level="analyse", outcome=t.outcome, rubric=rubric,
                                 answer={"model": m["correct_idea"]}, misconception_codes=[m["id"]],
                                 citation=[_cite(t.passages[0], t.passages[0]["body"][:200])] if t.passages else [])
        reasons = [(kind, c) for kind, _ in _REASON if kind != "result" for c in _clauses([p["body"] for p in t.passages], kind)]
        if n % 2 == 1 and reasons:  # every other question asks for a reason the textbook gives
            kind, (sentence, a, b) = reasons[(n // 2) % len(reasons)]
            src = next(p for p in t.passages if sentence in p["body"])
            words = [w for w in _content_words(b)][:4] or [b.split()[0]]
            prompt = f"Give a reason: {a}." if kind == "cause" else f"{a}. Why is this done?"
            return QuestionDraft(topic_id=t.id, kind="short", prompt=prompt, difficulty=difficulty,
                                 thinking_level="apply", outcome=t.outcome,
                                 rubric=[{"point": f"Gives the reason: {_cap(b)}.", "any": words},
                                         {"point": "Explains it clearly in a full sentence", "any": words[:2]}],
                                 answer={"model": sentence}, citation=[_cite(src, sentence)])
        m2 = n // 2 if reasons else n  # (half the questions on this topic were reasons)
        rubric = self._rubric_from_terms(t, 2 if difficulty == "easy" else 3, skip=m2)
        model, cites = self._model_answer(t, rubric)
        tail = " Answer in your own words." if difficulty == "easy" else " Use an example if you can."
        if m2 == 0 or not t.terms:
            prompt = t.outcome + tail
        else:  # more questions on the same topic: each centres on another key idea
            term = rubric[0]["any"][0]
            about = "" if t.title.rstrip().endswith("?") else f" in {t.title.lower()}"
            prompt = [f"What is meant by “{term}”{about}? Explain in your own words.",
                      f"Why is “{term}” important{about}?{tail}",
                      f"Describe what you learnt about “{term}”{about}.{tail}"][(m2 - 1) % 3]
        return QuestionDraft(topic_id=t.id, kind="short", prompt=prompt, difficulty=difficulty,
                             thinking_level=THINKING[difficulty], outcome=t.outcome, rubric=rubric,
                             answer={"model": model}, citation=cites)

    def _viva(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft:
        rubric = self._rubric_from_terms(t, 2, skip=n)
        own = [m for m in t.misconceptions if m.get("topic") == t.code]  # never another topic's question
        generic = [f"Give one example from daily life that shows this idea about {t.title.lower()}.",
                   f"Where have you seen this idea about {t.title.lower()} at home or in your town? Describe it.",
                   f"What would go wrong if people did not know about {t.title.lower()}? Give an example."]
        prompts = [m["example_prompt"] for m in own] + generic
        apply_q = prompts[n % len(prompts)]
        about = "" if t.title.rstrip().endswith("?") else f" in {t.title.lower()}"
        explain = t.outcome if n == 0 or not rubric else f"Explain “{rubric[0]['any'][0]}”{about}."
        rubric.append({"point": "Applies the idea to the new case with an example", "any": list(_EXAMPLE_MARKERS)})
        model, cites = self._model_answer(t, rubric[:-1])
        return QuestionDraft(topic_id=t.id, kind="viva", difficulty=difficulty, thinking_level="apply",
                             prompt=f"Part 1 (explain): {explain} Part 2 (apply): {apply_q}",
                             outcome=t.outcome, rubric=rubric, answer={"model": model}, citation=cites)

    def _ai_moment(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        if t.ai_moments:
            am = t.ai_moments[n % len(t.ai_moments)]
            return QuestionDraft(topic_id=t.id, kind="ai_moment", prompt=am["question"], scenario=am["claim"],
                                 difficulty=difficulty, thinking_level="analyse", outcome=t.outcome,
                                 options=list(am["options"]), answer={"index": am["answer"], "text": am["options"][am["answer"]]},
                                 ai_concept=am["concept"], citation=[], rubric=[{"point": am["explanation"], "any": []}])
        if not t.misconceptions:
            return None
        m = t.misconceptions[n % len(t.misconceptions)]
        right = f"Check it against the textbook: {m['correct_idea'].split('.')[0]}."
        options = ["Trust it. AI chatbots are always right.", right,
                   "Ask the same chatbot again until it agrees with you.", "Copy it into your notes as it is."]
        rng.shuffle(options)
        return QuestionDraft(topic_id=t.id, kind="ai_moment", prompt="What should the student do with this answer?",
                             scenario=f"An AI chatbot told a student: “{m['nova_says']}”", difficulty=difficulty,
                             thinking_level="analyse", outcome=t.outcome, options=options,
                             answer={"index": options.index(right), "text": right}, ai_concept="Checking AI claims",
                             misconception_codes=[m["id"]],
                             rubric=[{"point": "AI can be confidently wrong; check claims against trusted evidence.", "any": []}])

    def _teach(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft | None:
        if not t.misconceptions:
            return None
        codes = [m["id"] for m in t.misconceptions[:3]]
        return QuestionDraft(topic_id=t.id, kind="teach", prompt=f"Teach NOVA about {t.title}",
                             difficulty=difficulty, thinking_level="analyse", outcome=t.outcome,
                             misconception_codes=codes, ai_concept="Teaching an AI")

    def _learn(self, t: TopicCtx, difficulty: str, rng: random.Random, n: int) -> QuestionDraft:
        return QuestionDraft(topic_id=t.id, kind="learn", prompt=f"Learn: {t.title}", difficulty=difficulty,
                             thinking_level="remember", outcome=t.outcome,
                             citation=[_cite(p, p["body"][:200]) for p in t.passages[:1]])

    def _mission(self, topics: Sequence[TopicCtx], difficulty: str, count: int, rng: random.Random) -> list[QuestionDraft]:
        chapter = topics[0].chapter_title
        plan: list[QuestionDraft] = []
        for i, t in enumerate(topics):
            for use_mcq in (False, True):
                q = self._mcq(t, difficulty, rng, i) if use_mcq else self._cloze(t, "easy", rng, i)
                if q and all(q.prompt != o.prompt for o in plan):
                    plan.append(q)
        ai = next((q for q in (self._ai_moment(t, difficulty, rng, 0) for t in topics) if q), None)
        short = self._short(topics[-1], difficulty, rng, 0)
        body = plan[: max(1, count - 2)]
        if ai:
            body.append(ai)
        body.append(short)
        total = len(body)
        lines = [f"The NovaQuest lab robot is stuck on {chapter}. Help it with this step.",
                 "The first door opens. The next one has a lock that asks this.",
                 "The robot's sensor flickers: it isn't sure. Help it decide.",
                 "A screen lights up with a new puzzle.",
                 "Almost out! One more door stands between the robot and the exit.",
                 "The robot meets another robot that needs advice.",
                 "Final door: answer this to free the robot."]
        for i, q in enumerate(body, start=1):
            stage = f"Mission stage {i} of {total}. "
            line = lines[0] if i == 1 else (lines[-1] if i == total else lines[1 + (i - 2) % (len(lines) - 2)])
            q.scenario = stage + (q.scenario or line)
        return body[:count] if count >= 3 else body

    # ---------- marking ----------
    def mark_answer(self, q: dict, answer: dict | None, misconceptions: Sequence[dict] = ()) -> Mark:
        """Score one answer against its key or rubric, with a confidence level."""
        kind = q["kind"]
        if kind == "numeric":
            from novaquest.services import maths
            return maths.mark(q, answer)
        if kind in ("mcq", "ai_moment"):
            chosen = None if not answer else answer.get("index")
            correct = q["answer"]["index"]
            ok = chosen is not None and int(chosen) == int(correct)
            codes = tuple(q.get("misconception_codes") or []) if not ok else ()
            why = "Correct." if ok else f"The correct option is “{q['answer']['text']}”."
            return Mark(1.0 if ok else 0.0, 1.0, why, 1.0, codes)
        text = (answer or {}).get("text", "") if answer else ""
        rubric = q.get("rubric") or []
        maxs = float(max(len(rubric), 1))
        if not text.strip():
            return Mark(0.0, maxs, "No answer given.", 1.0)
        toks = _toks(text)
        hit = [r for r in rubric if any(phrase_in(a, toks) for a in r.get("any", []))]
        missed = [r for r in rubric if r not in hit]
        wrong = tuple(m["id"] for m in misconceptions if any(asserts(text, s) for s in m.get("wrong_signals", [])))
        score = float(len(hit)) - (0.5 if wrong else 0.0)
        non_latin = sum(1 for c in text if c.isalpha() and ord(c) > 0x24F) / max(1, sum(c.isalpha() for c in text))
        words = len(text.split())
        listing = keyword_list(text)
        if listing and hit:
            # Key words with no explanation ("shape amoeba size") can't earn more than a third, and a teacher checks it.
            score = min(score, round(maxs / 3, 2))
        score = max(0.0, min(maxs, score))
        if non_latin > 0.3:
            conf, note = 0.3, " Written in another language: please check."
        elif listing and hit:
            conf, note = 0.4, " Only lists key words without explaining them: please check."
        elif hit and missed:
            conf, note = 0.6, ""
        elif not hit and words >= 12:
            conf, note = 0.5, " Long answer without the expected key words: it may use different wording."
        elif not hit and words >= 6 and explains(text):
            conf, note = 0.5, " An explanation in the student's own words that doesn't match the marking points: please check."
        else:
            conf, note = 0.9, ""
        if wrong:
            conf = min(conf, 0.85)
        parts = []
        if hit:
            parts.append("Covered: " + "; ".join(r["point"] for r in hit) + ".")
        if missed:
            parts.append("Missing: " + "; ".join(r["point"] for r in missed) + ".")
        if wrong:
            parts.append("Shows a common mix-up: " + ", ".join(wrong) + ".")
        return Mark(round(score, 2), maxs, " ".join(parts) + note, conf, wrong)

    # ---------- tutor ----------
    def tutor_hint(self, q: dict, topic: TopicCtx, level: int, student_text: str = "") -> TutorReply:
        """A graded hint: 1 nudge, 2 pointer to the source, 3 worked step. Never the answer."""
        cite = q.get("citation") or []
        ref = cite[0]["ref"] if cite else (topic.passages[0]["ref"] if topic.passages else "your textbook")
        if level <= 1:
            if q["kind"] in ("mcq", "ai_moment"):
                text = "Read each option slowly and rule out the ones that don't fit. Which idea from this topic does the question test?"
            else:
                text = f"Start with the main idea of “{topic.title}”. What does it explain, and can you add an example?"
            return TutorReply(text, (ref,))
        if level == 2:
            quote = cite[0]["quote"] if cite else ""
            answers = [str((q.get("answer") or {}).get("text", ""))]
            for a in filter(None, answers):
                quote = re.sub(re.escape(a), "_____", quote, flags=re.I)
            text = f"Look at {ref}." + (f" Re-read: “{guard.keep_short(quote, 1, 160)}”" if quote else "")
            secrets = [str(v) for v in (q.get("answer") or {}).values() if isinstance(v, str)]
            if guard.leaks_answer(text, secrets):
                text = f"Look at {ref}. Re-read the part about {topic.title.lower()} and find the sentence that fits."
            return TutorReply(text, (ref,))
        rubric = q.get("rubric") or []
        if rubric and q["kind"] in ("short", "viva"):
            toks = _toks(student_text)
            missing = [r for r in rubric if r.get("any") and not any(phrase_in(a, toks) for a in r["any"])]
            if missing:
                return TutorReply(f"Step by step: your answer still needs this idea: {missing[0]['point'].lower()}. "
                                  "Write one sentence about it in your own words.", (ref,))
            return TutorReply("You have the key ideas. Now check that each sentence says why, not just what.", (ref,))
        return TutorReply("Worked step: cross out any option that is about a different topic, then compare the two "
                          f"that are left with what {ref} says.", (ref,))

    def answer_question(self, question: str, hits: Sequence[dict], topic_title: str | None, on_syllabus: bool,
                        course: str = "Grade 8 Science") -> TutorReply:
        """Answer from the retrieved passages only, citing them; decline off-syllabus questions."""
        if not on_syllabus or not hits:
            scope = f" about {topic_title}" if topic_title else " from your syllabus"
            return TutorReply(f"That question is outside your {course} syllabus, so I'll leave it. "
                              f"Ask me anything{scope}!", (), True)
        qt = set(_toks(question)) - _STOP
        asks_what = bool(re.search(r"\b(?:what'?s|whats|wat|what\s+(?:is|are)|define|meaning)\b", question.lower()))
        scored = []
        for h in hits[:3]:
            for s in _sentences(h["body"]):
                st = _toks(s)
                overlap = len(qt & set(st))
                if overlap:
                    # prefer sentences that say what the thing IS ("Viruses are…", "…is called…") and skip fragments
                    defines = asks_what and any(a in qt and b in ("is", "are") for a, b in zip(st, st[1:]))
                    score = overlap + (0.6 if defines else 0) - (1 if len(st) < 6 or not s[:1].isupper() else 0)
                    scored.append((score, -len(scored), s, h["ref"]))
        scored.sort(reverse=True)
        picked = scored[:2] or [(0, 0, _sentences(hits[0]["body"])[0] if _sentences(hits[0]["body"]) else hits[0]["body"][:200], hits[0]["ref"])]
        text = " ".join(s for _, _, s, _ in picked)
        refs = tuple(dict.fromkeys(r for _, _, _, r in picked))
        return TutorReply(guard.keep_short(text, 3, 420), refs)

    # ---------- Teach NOVA ----------
    def evaluate_teaching(self, m: dict, student_text: str, topic_terms: Sequence[str], already: Sequence[int] = (),
                          recent: Sequence[tuple[str, str]] = ()) -> TeachVerdict:
        """Judge whether the student's explanation fixes misconception `m`. `already`/`recent` (ideas taught earlier,
        the last few messages) are for Claude; here each message is judged on its own words."""
        text = student_text.strip()
        toks = _toks(text)
        low = text.lower()
        covered = tuple(i for i, grp in enumerate(m["key_ideas"]) if any(phrase_in(a, toks) for a in grp))
        has_example = any(mk in low for mk in _EXAMPLE_MARKERS) or bool(re.search(r"\d", text))
        # 'Yes' agrees with NOVA's wrong idea only when nothing else follows: 'yes, but not in absorbing water' and
        # 'yes, the root hairs…' go on to correct it
        all_ideas = len(covered) == len(m["key_ideas"])
        agrees = (bool(_SURE.match(text)) and not all_ideas) or (bool(_AGREE.match(text)) and not covered
                                                                 and not _CORRECTING.search(low))
        wrong = any(asserts(text, s) for s in m.get("wrong_signals", [])) or agrees
        on_topic = bool(covered) or any(phrase_in(t, toks) for t in topic_terms)
        if wrong:
            return TeachVerdict("incorrect", covered, has_example, "Explanation repeats or agrees with the wrong idea.")
        if not on_topic:
            return TeachVerdict("off_topic", covered, has_example, "Not about this topic.")
        if len(covered) == len(m["key_ideas"]) and len(toks) >= 6:
            return TeachVerdict("correct", covered, has_example, "Covers every key idea.")
        if covered:
            return TeachVerdict("partial", covered, has_example, "Some key ideas are still missing.")
        return TeachVerdict("vague", covered, has_example, "Too vague to change NOVA's mind.")

    # ---------- school books (SY-5) ----------
    def outline_chapter(self, course: str, chapter_title: str, lines: Sequence[str]) -> None:
        """Offline, topics come from the book's own headings (services.outline), so there is nothing to add here."""
        return None

    def suggest_note(self, info: dict) -> str:
        """A short, honest note to one student about their results (the teacher edits it before saving)."""
        name = info.get("first_name") or "Hi"
        strong = [c["name"] for c in info.get("topics", []) if c.get("pct", 0) >= 80]
        weak = [c["name"] for c in info.get("topics", []) if c.get("pct", 0) < 60]
        right, total = int(info.get("right", 0)), int(info.get("total", 0)) or 1
        if right == total:
            first = f"{name}, excellent work on {info.get('title', 'this task')}: you understood every idea."
        elif strong:
            first = f"{name}, well done on {strong[0]}: you clearly understand it."
        else:
            first = f"{name}, thank you for finishing {info.get('title', 'this task')}."
        if weak:
            nxt = f" Next, revise {weak[0]}: open it on Learn and go through the pictures and practice."
        elif info.get("mixups"):
            nxt = f" Watch out for this mix-up: {info['mixups'][0]}."
        else:
            nxt = " Keep going: try teaching NOVA the topic to make it stick."
        return first + nxt + (" I know you can do it!" if weak else "")

    def draft_escape_room(self, *_: Any, **__: Any) -> None:
        """Escape rooms for a school's own book need Claude."""
        return None

    def draft_unit_pack(self, *_: Any, **__: Any) -> None:
        """Teachers' prep packs are built from the book when Claude isn't there (services.prep)."""
        return None

    def draft_topic(self, topic: TopicCtx) -> None:
        """Picture lessons, quick checks and NOVA's mix-ups for a school's own book need Claude."""
        return None

    def nova_reply(self, m: dict, verdict: TeachVerdict, next_m: dict | None, topic_title: str,
                   ctx: dict | None = None) -> str:
        """NOVA's in-character reply. It never repeats a wrong idea back as fact, and (with the chat in `ctx`) doesn't
        ask the question it has just asked: it asks about an idea the student hasn't taught yet."""
        if ctx and verdict.verdict in ("partial", "vague", "incorrect") and ctx.get("missing"):
            last = next((b for r, b in reversed(ctx.get("recent") or []) if r == "nova"), "")
            ask = max(ctx["missing"][0].split(" / "), key=len)  # 'cell wall' rather than 'wall'
            if ctx.get("stuck"):
                return (f"Thanks for sticking with me! The part I still don't get is about \u201c{ask}\u201d. "
                        "Can you explain just that bit?")
            start = {"partial": "Okay, that part makes sense now.", "vague": "Why is that?",
                     "incorrect": "Wait, now I'm confused. That doesn't fit with what we saw in class."}[verdict.verdict]
            follow = m["example_prompt"] if verdict.verdict == "vague" else m["probe"]
            if follow not in last:
                return f"{start} {follow}"
            return f"{start} I'm still not sure about the part with \u201c{ask}\u201d. How does that fit in?"
        if verdict.verdict == "correct":
            learned = m["correct_idea"].split(".")[0]
            follow = f" But wait, I have another question. {next_m['nova_says']}" if next_m else \
                " I think I really get it now. Can I try the quiz?"
            return f"Ohh! So {learned[0].lower() + learned[1:]}. That makes sense!{follow}"
        if verdict.verdict == "partial":
            return f"Hmm, that helps a bit, but I'm still not sure. {m['probe']}"
        if verdict.verdict == "vague":
            return f"Why is that? {m['example_prompt']}"
        if verdict.verdict == "incorrect":
            return f"Wait, now I'm confused. That doesn't fit with what we saw in class. {m['probe']}"
        return f"Let's stay on {topic_title}. I still think this: {m['nova_says']}"

