"""Number problems (1.20.0): questions whose answer is a number, for maths and science.

A number question's key (questions.answer) holds the answer for the numbers shown to teachers, its unit, how close
an answer must be, step-by-step hints and a worked solution. It can also hold a template, so each student gets their
own numbers ("A rectangle is {l} cm by {b} cm…", l from 3 to 12): copying a neighbour's answer doesn't work, and the
marking, hints and worked solution all use that student's numbers. Marking is exact arithmetic, never AI:

- the student's answer is read the way students write it: 24, 24 cm², 24cm^2, 1,200, 3/4, 1 1/2, 2.5e3, 2.5 × 10^3,
  −5, 45%, ₹ 250;
- units are converted (1.2 m is right when the key says 120 cm);
- right number, missing or wrong unit: half marks and "Check the unit";
- close but outside the tolerance: half marks and "Check your rounding"; off by 10, 100 or 1000: "a unit slip?";
  the right size with the wrong sign: "check the sign".
"""
from __future__ import annotations

import ast
import hashlib
import math
import operator
import random
import re
from typing import Any

from novaquest.ai.types import Mark
from novaquest.core.errors import ValidationError

# ---------- units: name -> (dimension, factor to the base unit) ----------
_UNITS: dict[str, tuple[str, float]] = {
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "mm²": ("area", 1e-6), "cm²": ("area", 1e-4), "m²": ("area", 1.0), "km²": ("area", 1e6), "hectare": ("area", 1e4),
    "mm³": ("volume", 1e-9), "cm³": ("volume", 1e-6), "m³": ("volume", 1.0), "mL": ("volume", 1e-6), "L": ("volume", 1e-3),
    "mg": ("mass", 1e-6), "g": ("mass", 1e-3), "kg": ("mass", 1.0), "tonne": ("mass", 1000.0),
    "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0), "day": ("time", 86400.0),
    "m/s": ("speed", 1.0), "km/h": ("speed", 1 / 3.6), "cm/s": ("speed", 0.01),
    "m/s²": ("acceleration", 1.0), "N/kg": ("acceleration", 1.0),
    "g/cm³": ("density", 1000.0), "kg/m³": ("density", 1.0),
    "N": ("force", 1.0), "J": ("energy", 1.0), "kJ": ("energy", 1000.0), "W": ("power", 1.0), "kW": ("power", 1000.0),
    "Pa": ("pressure", 1.0), "kPa": ("pressure", 1000.0), "N/m²": ("pressure", 1.0),
    "V": ("voltage", 1.0), "A": ("current", 1.0), "mA": ("current", 0.001), "Ω": ("resistance", 1.0),
    "°C": ("temperature", 1.0), "°": ("angle", 1.0), "%": ("percent", 1.0), "₹": ("money", 1.0),
}
_ALIASES = {
    "sq cm": "cm²", "sqcm": "cm²", "cm2": "cm²", "cm^2": "cm²", "square cm": "cm²", "square centimetres": "cm²",
    "sq m": "m²", "sqm": "m²", "m2": "m²", "m^2": "m²", "square metres": "m²", "square meters": "m²",
    "mm2": "mm²", "mm^2": "mm²", "km2": "km²", "km^2": "km²", "ha": "hectare", "hectares": "hectare",
    "cm3": "cm³", "cm^3": "cm³", "cc": "cm³", "cubic cm": "cm³", "m3": "m³", "m^3": "m³", "mm3": "mm³", "mm^3": "mm³",
    "ml": "mL", "millilitre": "mL", "millilitres": "mL", "l": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L",
    "metre": "m", "metres": "m", "meter": "m", "meters": "m", "centimetre": "cm", "centimetres": "cm", "centimeter": "cm",
    "centimeters": "cm", "millimetre": "mm", "millimetres": "mm", "kilometre": "km", "kilometres": "km", "kilometer": "km",
    "kilometers": "km", "gram": "g", "grams": "g", "gm": "g", "gms": "g", "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "milligram": "mg", "milligrams": "mg", "tonnes": "tonne", "ton": "tonne", "tons": "tonne",
    "sec": "s", "secs": "s", "second": "s", "seconds": "s", "mins": "min", "minute": "min", "minutes": "min",
    "hr": "h", "hrs": "h", "hour": "h", "hours": "h", "days": "day",
    "m/s2": "m/s²", "m/s^2": "m/s²", "ms-2": "m/s²", "m s-2": "m/s²", "n/kg": "N/kg",
    "kmph": "km/h", "km/hr": "km/h", "kmh": "km/h", "km per hour": "km/h", "mps": "m/s", "m per s": "m/s",
    "g/cm3": "g/cm³", "g/cc": "g/cm³", "kg/m3": "kg/m³", "newton": "N", "newtons": "N", "joule": "J", "joules": "J",
    "watt": "W", "watts": "W", "pascal": "Pa", "volt": "V", "volts": "V", "amp": "A", "amps": "A", "ampere": "A",
    "amperes": "A", "ohm": "Ω", "ohms": "Ω", "degc": "°C", "° c": "°C", "c": "°C", "degrees celsius": "°C", "celsius": "°C",
    "deg": "°", "degree": "°", "degrees": "°", "percent": "%", "per cent": "%", "rs": "₹", "rs.": "₹", "rupees": "₹",
    "rupee": "₹", "inr": "₹",
}
_CASED = {"L", "N", "J", "W", "V", "A", "Pa", "kPa", "kJ", "kW", "mA", "mL", "N/m²"}


def unit_of(text: str) -> str | None:
    """The canonical unit for what a student wrote ('sq cm' -> 'cm²'), or None if it isn't one we know."""
    t = " ".join(text.strip().split())
    if not t:
        return None
    if t in _UNITS:
        return t
    low = t.lower().rstrip(".") if t.lower() not in ("rs.",) else t.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    for u in _UNITS:
        if u not in _CASED and u.lower() == low:
            return u
    return None


# ---------- reading a number ----------
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def parse(text: str) -> tuple[float | None, str | None, str]:
    """(value, unit, rest) from a student's answer. value None: no number found. rest: unit text we didn't know."""
    s = str(text or "").strip()
    s = s.replace("−", "-").replace("–", "-").replace("×", "x").replace("·", ".").replace(" ", " ")
    s = re.sub(r"^(?:answer|ans|=)\s*[:=]?\s*", "", s, flags=re.I)
    if "=" in s:  # '24 = 6 x 4' or 'A = 24 cm²': the part after the last '='
        s = s.rsplit("=", 1)[1].strip()
    money = None
    m = re.match(r"^(₹|rs\.?|inr)\s*", s, flags=re.I)
    if m:
        money, s = "₹", s[m.end():]
    s = re.sub(r"^(?:about|approx\.?|approximately|around|nearly|roughly|it is|it's|its)\s+", "", s, flags=re.I)
    s = re.sub(r"(?<=\d),(?=\d{2,3}(?:,|\.|\b))", "", s)  # 1,200 and the Indian 1,20,000
    value: float | None = None
    patterns = [
        (rf"^({_NUM})\s*x\s*10\s*\^?\s*\(?\s*([-+]?\d+)\s*\)?", lambda a, b: float(a) * 10 ** int(b)),
        (rf"^({_NUM})\s*x\s*10([⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+)", lambda a, b: float(a) * 10 ** int(b.translate(_SUP))),
        (r"^([-+]?\d+)\s+(\d+)\s*/\s*(\d+)", lambda a, b, c: (abs(int(a)) + int(b) / int(c)) * (-1 if a.startswith("-") else 1)
         if int(c) else None),
        (r"^([-+]?\d+)\s*/\s*(\d+)", lambda a, b: int(a) / int(b) if int(b) else None),
        (rf"^({_NUM}[eE][-+]?\d+)", lambda a: float(a)),
        (rf"^({_NUM})\s*(?:π|pi)\b", lambda a: float(a) * math.pi),
        (r"^(?:π|pi)\b", lambda: math.pi),
        (rf"^({_NUM})", lambda a: float(a)),
    ]
    rest = s
    for pat, fn in patterns:
        m = re.match(pat, s, flags=re.I)
        if m:
            value = fn(*m.groups())
            rest = s[m.end():]
            break
    rest = rest.strip().replace("^2", "²").replace("^3", "³")
    unit = money or unit_of(rest) or (unit_of(rest.split()[0]) if rest.split() else None)
    return value, unit, ("" if unit else rest)


# ---------- safe arithmetic for templates ----------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_FUNCS = {"int": int, "sqrt": math.sqrt, "round": round, "abs": abs, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil}
_CONSTS = {"pi": math.pi}


def evaluate(formula: str, values: dict[str, float]) -> float:
    """Work out a formula like 'l*b' or '2*pi*r' with the given numbers. Only arithmetic is allowed."""
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in values:
                return values[n.id]
            if n.id in _CONSTS:
                return _CONSTS[n.id]
            raise ValueError(f"unknown name {n.id}")
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 10:
                raise ValueError("power too big")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.List) and len(n.elts) <= 12:
            return [ev(e) for e in n.elts]  # type: ignore[return-value]
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.List):
            items, i = ev(n.value), int(ev(n.slice))
            return items[i]  # type: ignore[index]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("not allowed")
    if len(formula) > 200:
        raise ValueError("formula too long")
    out = ev(ast.parse(formula.replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval"))
    if not isinstance(out, (int, float)) or math.isnan(out) or math.isinf(out):
        raise ValueError("not a number")
    return float(out)


def show(x: float, unit: str, decimals: int | None = None) -> str:
    """24 cm², ₹250, 45%, 65°: a number with its unit, written the usual way."""
    n = fmt(x, decimals)
    if unit == "₹":
        return f"₹{n}"
    return f"{n}{unit}" if unit in ("%", "°") else (f"{n} {unit}" if unit else n)


def fmt(x: float, decimals: int | None = None) -> str:
    """24.0 -> '24', 2.5 -> '2.5', 1200 -> '1200', with at most `decimals` places."""
    if decimals is not None:
        x = round(x, decimals)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.{decimals if decimals is not None else 4}f}".rstrip("0").rstrip(".")


def _fill(text: str, values: dict[str, float], answer: float, unit: str, decimals: int | None) -> str:
    def rep(m: re.Match) -> str:
        key = m.group(1)
        if key.endswith(":sup"):  # {b:sup} -> a power, printed raised: 2⁵
            inner = rep(re.match(r"\{(.*)\}", "{" + key[:-4] + "}"))  # type: ignore[arg-type]
            return inner.translate(str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻"))
        if key == "answer":
            return fmt(answer, decimals)
        if key == "unit":
            return unit
        if key in values:
            return fmt(values[key])
        try:  # {l*b} works out a step's arithmetic
            return fmt(evaluate(key, values), decimals)
        except (ValueError, SyntaxError, ZeroDivisionError, TypeError):
            return m.group(0)
    return re.sub(r"\{([^{}]{1,60})\}", rep, text or "")


def _draw(spec: Any, rnd: random.Random) -> float:
    if isinstance(spec, list) and len(spec) in (2, 3) and all(isinstance(v, (int, float)) for v in spec) and spec[0] <= spec[1]:
        lo, hi = spec[0], spec[1]
        step = spec[2] if len(spec) == 3 and spec[2] else 1
        n = int(round((hi - lo) / step))
        return round(lo + step * rnd.randint(0, max(n, 0)), 6)
    if isinstance(spec, dict) and isinstance(spec.get("from"), list) and spec["from"]:
        return float(rnd.choice(spec["from"]))
    raise ValueError("a number range is [from, to] or [from, to, step]")


def instance(key: dict, seed: int) -> dict[str, Any] | None:
    """The question's numbers for one seed: {'values', 'value', 'prompt', 'steps', 'worked'}, from its template. None
    if the question has no template (its numbers are fixed)."""
    t = (key or {}).get("template")
    if not t:
        return None
    rnd = random.Random(seed)
    unit, dec = key.get("unit") or "", key.get("decimals")
    for _ in range(30):
        values = {k: _draw(v, rnd) for k, v in sorted(t["vars"].items())}
        try:
            for k, f in (t.get("derived") or {}).items():
                values[k] = evaluate(f, values)
            value = evaluate(t["formula"], values)
        except (ValueError, ZeroDivisionError, OverflowError, IndexError, TypeError):
            continue
        if t.get("whole") and abs(value - round(value)) > 1e-9:
            continue
        if t.get("positive") and value <= 0:
            continue
        if t.get("differ") and len({values[k] for k in t["differ"]}) < len(t["differ"]):
            continue
        return {"values": values, "value": value, "prompt": _fill(t["prompt"], values, value, unit, dec),
                "steps": [_fill(s, values, value, unit, dec) for s in t.get("steps", [])],
                "worked": _fill(t.get("worked", ""), values, value, unit, dec)}
    return None


def seed_for(question_id: int, student_id: int) -> int:
    return int(hashlib.sha256(f"{question_id}:{student_id}".encode()).hexdigest()[:8], 16)


def for_student(q: dict, student_id: int) -> dict:
    """A copy of a number question with this student's own numbers (prompt and key). Other questions unchanged."""
    if q.get("kind") != "numeric":
        return q
    key = q.get("answer") or {}
    inst = instance(key, seed_for(q["id"], student_id))
    if not inst:
        return q
    return {**q, "prompt": inst["prompt"],
            "answer": {**key, "value": inst["value"], "steps": inst["steps"], "worked": inst["worked"]}}


# ---------- marking ----------
def _close(a: float, b: float, key: dict) -> bool:
    if key.get("abs") is not None:
        return abs(a - b) <= float(key["abs"]) + 1e-9
    rel = float(key.get("tol", 0.01))
    return abs(a - b) <= max(rel * abs(b), 1e-9 if b == 0 else 0)


def mark(q: dict, answer: dict | None) -> Mark:
    """Mark a number answer against its key (already this student's numbers). Out of 1."""
    key = q.get("answer") or {}
    want, kunit, dec = float(key["value"]), key.get("unit") or "", key.get("decimals")
    shown = show(want, kunit, dec)
    text = ((answer or {}).get("text") or "").strip()
    if not text:
        return Mark(0.0, 1.0, "No answer given.", 1.0)
    value, unit, junk = parse(text)
    if value is None:
        return Mark(0.0, 1.0, f"No number found in the answer. The answer is {shown}.", 0.6)
    got = value
    unit_ok = True
    if kunit and unit and unit != kunit:
        ku, su = _UNITS.get(kunit), _UNITS.get(unit)
        if ku and su and ku[0] == su[0] and ku[0] != "temperature":
            got = value * su[1] / ku[1]
        else:
            unit_ok = False
    if kunit and not unit and kunit not in ("₹",):
        unit_ok = False
    if _close(got, want, key):
        if unit_ok:
            return Mark(1.0, 1.0, "Correct.", 1.0)
        note = f"The number is right, but check the unit: the answer is {shown}." if unit or junk else \
            f"The number is right; remember the unit: {shown}."
        return Mark(0.5, 1.0, note, 1.0)
    if want and got and any(_close(got * f, want, key) or _close(got / f, want, key) for f in (10, 100, 1000)):
        return Mark(0.0, 1.0, f"Not quite: off by a power of ten. A unit slip? The answer is {shown}.", 1.0)
    if want and _close(-got, want, key):
        return Mark(0.0, 1.0, f"The size is right but check the sign. The answer is {shown}.", 1.0)
    if want and abs(got - want) <= 0.05 * abs(want):
        return Mark(0.5 if unit_ok else 0.25, 1.0, f"Close: check your rounding. The answer is {shown}.", 0.9)
    return Mark(0.0, 1.0, f"Not right. The answer is {shown}.", 1.0)


# ---------- teacher input and validation ----------
def parse_vars(text: str) -> dict[str, list[float]]:
    """'l = 3..12, b = 2..9 step 1, r = 1.5..4.5 step 0.5' -> {'l': [3, 12], 'b': [2, 9, 1], 'r': [1.5, 4.5, 0.5]}"""
    out: dict[str, list[float]] = {}
    for part in re.split(r"[,;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(rf"^([a-zA-Z]\w{{0,11}})\s*(?:=|:|from)\s*({_NUM})\s*(?:\.\.|to|-)\s*({_NUM})(?:\s*(?:step|by)\s*({_NUM}))?$",
                     part, flags=re.I)
        if not m:
            raise ValidationError(f"Write each number range like “l = 3..12” or “r = 1.5..4.5 step 0.5” (couldn't read “{part}”).",
                                  field="vars")
        name, lo, hi, step = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4)
        if lo > hi:
            lo, hi = hi, lo
        out[name] = [lo, hi] + ([float(step)] if step else [])
    return out


def build_key(*, value: str = "", unit: str = "", tolerance: str = "", formula: str = "", vars_text: str = "",
              prompt: str, steps: list[str], worked: str, whole: bool = False) -> tuple[str, dict]:
    """A number question's (prompt shown to teachers, key) from the teacher's form. With number ranges and a formula
    each student gets their own numbers; otherwise the answer is the number given."""
    u = unit.strip()
    kunit = (unit_of(u) or u) if u else ""
    tol: dict[str, float] = {}
    t = tolerance.strip().replace(" ", "")
    if t:
        m = re.match(r"^±?([\d.]+)(%?)$", t)
        if not m:
            raise ValidationError("Write how close an answer must be like “0.5” or “1%”.", field="tolerance")
        tol = {"tol": float(m.group(1)) / 100} if m.group(2) else {"abs": float(m.group(1))}
    steps = [s.strip() for s in steps if s.strip()][:5]
    decimals = max(0, -math.floor(math.log10(tol["abs"]))) if tol.get("abs") else 2
    key: dict[str, Any] = {"unit": kunit, **tol, "decimals": decimals, "steps": steps, "worked": worked.strip()}
    if formula.strip() or vars_text.strip():
        variables = parse_vars(vars_text)
        if not variables:
            raise ValidationError("Give at least one number range, like “l = 3..12”.", field="vars")
        missing = [v for v in variables if "{" + v + "}" not in prompt]
        if missing:
            raise ValidationError(f"Put {{{missing[0]}}} in the question where its number goes.", field="prompt")
        key["template"] = {"prompt": prompt.strip(), "vars": variables, "formula": formula.strip(),
                           "steps": steps, "worked": worked.strip(), "whole": whole}
        sample = instance(key, 0)
        if not sample:
            raise ValidationError("The formula doesn't give a number for these ranges. Check it (use * for ×).", field="formula")
        key.update(value=sample["value"], steps=sample["steps"], worked=sample["worked"])
        return sample["prompt"], validate(key)
    v, vu, _ = parse(value)
    if v is None:
        raise ValidationError("Give the answer as a number.", field="value")
    if vu and not kunit:
        key["unit"] = vu
    key["value"] = v
    return prompt.strip(), validate(key)


def validate(key: dict) -> dict:
    """Check a number key (from a teacher or from Claude) and return it cleaned; ValidationError if it can't work.
    Hints must not give the answer away."""
    if not isinstance(key.get("value"), (int, float)) or math.isnan(float(key["value"])):
        raise ValidationError("The answer must be a number.")
    t = key.get("template")
    if t:
        whole = re.sub(r"\s", "", t["formula"])
        if any("{answer}" in s or (len(whole) > 1 and "{" + whole + "}" in re.sub(r"\s", "", s)) for s in t.get("steps", [])):
            raise ValidationError("A hint gives the answer away. Hints should show the method, not the result.")
        for i in range(5):
            if not instance(key, i):
                raise ValidationError("The formula doesn't give a number for these ranges.")
    else:
        ans = fmt(float(key["value"]), key.get("decimals"))
        if any(re.search(rf"(?<![\d.]){re.escape(ans)}(?![\d.])", s) for s in key.get("steps", [])):
            raise ValidationError("A hint gives the answer away. Hints should show the method, not the result.")
    if key.get("unit") and key["unit"] not in _UNITS:
        key["unit"] = str(key["unit"])[:20]
    return key


# ---------- ready-made number problems (used without Claude) ----------
# Each fits topics whose words match `when`. Prompts, hints and worked solutions take each student's own numbers.
LIBRARY: list[dict[str, Any]] = [
    {"when": r"\barea\b.*\brectangle|\brectangle.*\barea\b|\bmensuration\b", "unit": "cm²", "level": "apply",
     "prompt": "A rectangular card is {l} cm long and {b} cm wide. What is its area?",
     "vars": {"l": [4, 15], "b": [2, 9]}, "formula": "l*b", "whole": True,
     "steps": ["Area of a rectangle = length × breadth.", "Multiply {l} × {b}, and give the unit (square centimetres)."],
     "worked": "Area = length × breadth = {l} cm × {b} cm = {answer} cm²."},
    {"when": r"\bperimeter\b|\bmensuration\b", "unit": "m", "level": "apply",
     "prompt": "A rectangular garden bed is {l} m long and {b} m wide. How many metres of fencing go all the way round it?",
     "vars": {"l": [5, 20], "b": [3, 12]}, "formula": "2*(l+b)", "whole": True,
     "steps": ["The fence goes round the perimeter: perimeter = 2 × (length + breadth).", "First add {l} + {b}, then double it."],
     "worked": "Perimeter = 2 × ({l} + {b}) = 2 × {l+b} = {answer} m."},
    {"when": r"\btriangle\b.*\barea\b|\barea\b.*\btriangle", "unit": "cm²", "level": "apply",
     "prompt": "A triangle has a base of {b} cm and a height of {h} cm. What is its area?",
     "vars": {"b": [4, 16, 2], "h": [3, 12]}, "formula": "b*h/2",
     "steps": ["Area of a triangle = ½ × base × height.", "Work out {b} × {h}, then halve it."],
     "worked": "Area = ½ × {b} × {h} = ½ × {b*h} = {answer} cm²."},
    {"when": r"\bcircle\b|\bcircumference\b|\bradius\b", "unit": "cm", "level": "apply", "tolerance": 0.1,
     "prompt": "A wheel has a radius of {r} cm. How far does it roll in one full turn? (Use π = 3.14; give your answer to 1 decimal place.)",
     "vars": {"r": [7, 35, 7]}, "formula": "2*3.14*r", "slips": ["3.14*r", "3.14*r*r", "2*r"],
     "steps": ["One full turn rolls the wheel along its circumference: C = 2πr.", "Work out 2 × 3.14 × {r}."],
     "worked": "C = 2πr = 2 × 3.14 × {r} = {answer} cm."},
    {"when": r"\bspeed\b|\bdistance\b.*\btime\b|\bmotion\b", "unit": "km/h", "level": "apply",
     "prompt": "A bus travels {d} km in {t} hours at a steady speed. What is its speed?",
     "vars": {"t": [2, 5], "v": [30, 60, 5]}, "formula": "v", "derived": {"d": "v*t"}, "whole": True,
     "steps": ["Speed = distance ÷ time.", "Divide {d} km by {t} h."],
     "worked": "Speed = distance ÷ time = {d} km ÷ {t} h = {answer} km/h."},
    {"when": r"\bdensity\b|\bfloat\b.*\bsink\b|\bsink\b.*\bfloat\b", "unit": "g/cm³", "level": "apply", "tolerance": 0.01,
     "prompt": "A block has a mass of {m} g and a volume of {v} cm³. What is its density? (Give your answer to 2 decimal places.)",
     "vars": {"v": [10, 50, 5], "k": [0.5, 3, 0.1]}, "formula": "m/v", "derived": {"m": "round(k*v)"},
     "steps": ["Density = mass ÷ volume.", "Divide {m} g by {v} cm³."],
     "worked": "Density = mass ÷ volume = {m} g ÷ {v} cm³ = {answer} g/cm³."},
    {"when": r"\bpressure\b", "unit": "Pa", "level": "apply",
     "prompt": "A box presses on the floor with a force of {f} N over an area of {a} m². What pressure does it put on the floor?",
     "vars": {"a": [2, 5], "p": [20, 200, 10]}, "formula": "p", "derived": {"f": "p*a"}, "whole": True,
     "steps": ["Pressure = force ÷ area.", "Divide {f} N by {a} m²."],
     "worked": "Pressure = force ÷ area = {f} N ÷ {a} m² = {answer} Pa (N/m²)."},
    {"when": r"\bohm\b|\bresistance\b|\belectric current\b|\bvoltage\b|\bcircuit\b", "unit": "A", "level": "apply",
     "prompt": "A {v} V battery is connected to a {r} Ω resistor. What current flows through it?",
     "vars": {"r": [2, 12], "i": [0.5, 3, 0.5]}, "formula": "i", "derived": {"v": "i*r"},
     "steps": ["Ohm's law: V = I × R, so I = V ÷ R.", "Divide {v} V by {r} Ω."],
     "worked": "I = V ÷ R = {v} V ÷ {r} Ω = {answer} A."},
    {"when": r"\bpercent|\bpercentage\b", "unit": "", "level": "apply",
     "prompt": "{p}% of the {n} students in a school walk to school. How many students walk?",
     "vars": {"p": [10, 90, 5], "n": [200, 1200, 40]}, "formula": "p*n/100", "whole": True, "slips": ["p*n/10", "n-p", "n/p"],
     "steps": ["{p}% means {p} out of every 100.", "Work out {p} ÷ 100 × {n}."],
     "worked": "{p}% of {n} = {p} ÷ 100 × {n} = {answer} students."},
    {"when": r"\binterest\b", "unit": "₹", "level": "apply",
     "prompt": "Priya puts ₹{p} in a bank at {r}% simple interest a year. How much interest does she earn in {t} years?",
     "vars": {"p": [1000, 20000, 500], "r": [4, 10], "t": [2, 5]}, "formula": "p*r*t/100", "whole": True,
     "steps": ["Simple interest = P × R × T ÷ 100.", "Multiply {p} × {r} × {t}, then divide by 100."],
     "worked": "SI = P × R × T ÷ 100 = {p} × {r} × {t} ÷ 100 = ₹{answer}."},
    {"when": r"\baverage\b|\bmean\b|\bdata handling\b|\bstatistics\b", "unit": "", "level": "apply",
     "prompt": "Five students scored {a}, {b}, {c}, {d} and {e} in a test. What is the mean score?",
     "vars": {"a": [30, 95], "b": [30, 95], "c": [30, 95], "d": [30, 95], "e": [30, 95]}, "formula": "(a+b+c+d+e)/5",
     "steps": ["Mean = total of the scores ÷ number of scores.", "Add the five scores, then divide by 5."],
     "worked": "Mean = ({a} + {b} + {c} + {d} + {e}) ÷ 5 = {a+b+c+d+e} ÷ 5 = {answer}."},
    {"when": r"\bvolume\b|\bcuboid\b|\bcapacity\b", "unit": "cm³", "level": "apply",
     "prompt": "A box is {l} cm long, {b} cm wide and {h} cm high. What is its volume?",
     "vars": {"l": [5, 20], "b": [3, 12], "h": [2, 10]}, "formula": "l*b*h", "whole": True,
     "steps": ["Volume of a cuboid = length × breadth × height.", "Work out {l} × {b} first, then multiply by {h}."],
     "worked": "Volume = {l} × {b} × {h} = {l*b} × {h} = {answer} cm³."},
    {"when": r"\bwork done\b|\bwork and energy\b|\benergy\b.*\bforce\b", "unit": "J", "level": "apply",
     "prompt": "A girl pushes a trolley with a force of {f} N for {s} m. How much work does she do?",
     "vars": {"f": [10, 80, 5], "s": [2, 20]}, "formula": "f*s", "whole": True,
     "steps": ["Work done = force × distance moved in the direction of the force.", "Multiply {f} N by {s} m."],
     "worked": "W = F × s = {f} N × {s} m = {answer} J."},
    {"when": r"\bacceleration\b", "unit": "m/s²", "level": "apply",
     "prompt": "A car speeds up from {u} m/s to {v} m/s in {t} s. What is its acceleration?",
     "vars": {"u": [0, 10], "a": [1, 4], "t": [2, 8]}, "formula": "a", "derived": {"v": "u+a*t"}, "whole": True,
     "steps": ["Acceleration = change in speed ÷ time.", "Change in speed = {v} − {u}; divide it by {t}."],
     "worked": "a = (v − u) ÷ t = ({v} − {u}) ÷ {t} = {v-u} ÷ {t} = {answer} m/s²."},
    {"when": r"\bpythagoras\b|\bhypotenuse\b|\bright[- ]angled\b", "unit": "cm", "level": "apply",
     "prompt": "A right-angled triangle has shorter sides of {a} cm and {b} cm. How long is the longest side?",
     "vars": {"k": [1, 4], "t": {"from": [0, 1, 2]}}, "formula": "k*[5,13,17][int(t)]",
     "derived": {"a": "k*[3,5,8][int(t)]", "b": "k*[4,12,15][int(t)]"}, "whole": True,
     "steps": ["Pythagoras: hypotenuse² = a² + b².", "Work out {a}² + {b}², then take the square root."],
     "worked": "c² = {a}² + {b}² = {a*a} + {b*b} = {a*a+b*b}, so c = √{a*a+b*b} = {answer} cm."},
    {"when": r"\blinear equation|\bequations? in one variable|\bsolve\b.*\bequation", "unit": "", "level": "apply",
     "prompt": "Solve for x: {a}x + {b} = {c}",
     "vars": {"a": [2, 9], "x": [-6, 12], "b": [1, 20]}, "formula": "x", "derived": {"c": "a*x+b"}, "whole": True,
     "steps": ["Take {b} from both sides: {a}x = {c} − {b}.", "Then divide both sides by {a}."],
     "worked": "{a}x = {c} − {b} = {c-b}, so x = {c-b} ÷ {a} = {answer}."},
    {"when": r"\bweight\b|\bgravit", "unit": "N", "level": "apply",
     "prompt": "A bag has a mass of {m} kg. What is its weight on Earth? (Take g = 10 N/kg.)",
     "vars": {"m": [2, 40]}, "formula": "m*10", "whole": True,
     "steps": ["Weight = mass × g.", "Multiply {m} kg by 10 N/kg."],
     "worked": "W = m × g = {m} kg × 10 N/kg = {answer} N."},
]


def _key_from(t: dict, sample: int = 0) -> dict:
    """A key (with its template) from a library entry or from Claude's JSON. `sample` picks the numbers teachers see."""
    tol = t.get("tolerance")
    decimals = 0 if t.get("whole") else (max(0, -math.floor(math.log10(float(tol)))) if tol else 2)
    key: dict[str, Any] = {"unit": unit_of(t.get("unit") or "") or (t.get("unit") or ""), "decimals": decimals,
                           "template": {k: t[k] for k in ("prompt", "vars", "formula", "steps", "worked", "whole", "derived", "positive", "differ")
                                        if k in t}}
    if tol:
        key["abs"] = float(tol)
    key["template"].setdefault("steps", [])
    key["template"].setdefault("worked", "")
    if t.get("slips"):
        key["slips"] = list(t["slips"])
    sample = instance(key, sample) or instance(key, 0)
    if not sample:
        raise ValidationError("The formula doesn't give a number for these ranges.")
    key.update(value=sample["value"], steps=sample["steps"], worked=sample["worked"])
    return validate(key)


def from_library(text: str, count: int, seed: int) -> list[tuple[str, dict, str]]:
    """Up to `count` ready-made number problems that fit a topic's text: (prompt for teachers, key, thinking level)."""
    fits = _fits(text, random.Random(seed))
    if not fits:
        return []
    out = []
    for i in range(min(count, len(fits) * (2 if count <= 10 else 5))):  # an exam paper needs more, from fewer ideas
        t = fits[i % len(fits)]
        key = _key_from(t, sample=seed % 97 + i)  # a second question from the same idea shows other numbers
        out.append((_sample_prompt(key, seed % 97 + i), key, t.get("level", "apply")))
    return out


def _sample_prompt(key: dict, sample: int) -> str:
    inst = instance(key, sample) or instance(key, 0)
    return inst["prompt"] if inst else ""


def _fits(text: str, rnd: random.Random | None = None) -> list[dict]:
    """The ready-made problems that fit, the most specific first ('powers of 10' before 'powers')."""
    hits = []
    for t in LIBRARY:
        m = re.search(t["when"], text, flags=re.I | re.S)
        if m:
            hits.append((len(m.group(0)), (rnd or random.Random(0)).random(), t))
    return [t for _, _, t in sorted(hits, key=lambda h: (-h[0], h[1]))]


def mcq_from_library(text: str, count: int, seed: int) -> list[dict]:
    """Multiple-choice questions from the ready-made problems that fit a topic: the right answer and three wrong ones
    from the usual slips (2 × 5 instead of 5², adding instead of multiplying, a power of ten out, a sign), so a maths
    quiz or Daily 5 works without Claude. Each {prompt, options, answer, why, level}."""
    rnd = random.Random(seed)
    fits = _fits(text, rnd)
    out: list[dict] = []
    for i in range(min(count, len(fits) * 2)):
        q = mcq_for(fits[i % len(fits)], seed % 89 + i, rnd)
        if q:
            out.append(q)
    return out


def mcq_for(t: dict, sample: int, rnd: random.Random | None = None) -> dict | None:
    """One ready-made problem as multiple choice, with the numbers of `sample`. None if it can't make four options."""
    rnd = rnd or random.Random(sample)
    key = _key_from(t, sample=sample)
    inst = instance(key, sample)
    if not inst:
        return None
    unit, dec = key.get("unit") or "", key.get("decimals")
    right = inst["value"]
    wrong: list[float] = []
    for f in t.get("slips", []):
        try:
            wrong.append(evaluate(f, inst["values"]))
        except (ValueError, ZeroDivisionError, OverflowError, IndexError, TypeError):
            continue
    wrong += [right + 10, right - 10, right * 2, right + 1, right - 1, right * 10]
    opts = [show(right, unit, dec)]
    for v in wrong:
        s = show(v, unit, dec)
        exact = abs(float(fmt(v, dec)) - v) <= 0.02 * abs(v)  # no option rounded into another number ('0 cm')
        if s not in opts and exact and (v >= 0 or right < 0) and abs(v - right) > 1e-9:
            opts.append(s)
        if len(opts) == 4:
            break
    if len(opts) < 4:
        return None
    order = list(range(4))
    rnd.shuffle(order)
    options = [opts[k] for k in order]
    return {"prompt": inst["prompt"], "options": options, "answer": order.index(0), "why": inst["worked"],
            "level": t.get("level", "apply")}


def is_maths_course(course: str | None) -> bool:
    return bool(re.search(r"\bmath|ganit", course or "", re.I))


def from_claude(q: dict) -> tuple[str, dict]:
    """A number question Claude wrote ({prompt, vars, formula, unit, tolerance, whole, steps, worked}), checked."""
    vars_ = q.get("vars") or {}
    if not isinstance(vars_, dict) or not vars_ or not all(isinstance(v, list) and len(v) in (2, 3) for v in vars_.values()):
        raise ValidationError("number ranges missing")
    t = {"prompt": str(q["prompt"])[:600], "vars": {str(k)[:12]: [float(x) for x in v] for k, v in list(vars_.items())[:6]},
         "formula": str(q["formula"])[:200], "unit": str(q.get("unit") or "")[:20], "whole": bool(q.get("whole")),
         "steps": [str(s)[:200] for s in (q.get("steps") or [])][:4], "worked": str(q.get("worked") or "")[:500]}
    if q.get("tolerance"):
        t["tolerance"] = float(q["tolerance"])
    if any("{" + v + "}" not in t["prompt"] for v in t["vars"]):
        raise ValidationError("a number range isn't used in the question")
    key = _key_from(t)
    return instance(key, 0)["prompt"], key


# ---------- more ready-made problems: Grade 8 maths (1.24.0) ----------
# 'slips' are the usual wrong workings, used as the wrong options when a problem becomes multiple choice.
LIBRARY += [
    {"when": r"\bsquare numbers?\b|\bsquares?\b.*\bnumber|\bperfect squares?", "unit": "", "level": "apply",
     "prompt": "What is {n}²?", "vars": {"n": [11, 35]}, "formula": "n*n", "whole": True, "slips": ["2*n", "n*10", "n*n+n"],
     "steps": ["{n}² means {n} × {n}.", "Multiply {n} by itself."], "worked": "{n}² = {n} × {n} = {answer}."},
    {"when": r"\bsquare roots?\b|\bperfect squares?|\bsquare numbers?", "unit": "", "level": "apply",
     "prompt": "The area of a square tile is {s} cm². How long is each side?", "vars": {"n": [6, 25]},
     "derived": {"s": "n*n"}, "formula": "n", "unit_": "cm", "whole": True, "slips": ["s/2", "s/4", "n*2"],
     "steps": ["Area of a square = side × side, so the side is the square root of the area.",
               "Which number times itself gives {s}?"], "worked": "√{s} = {answer}, since {answer} × {answer} = {s}. Each side is {answer} cm."},
    {"when": r"\bcubes?\b|\bcubic numbers?|\bcube roots?", "unit": "", "level": "apply",
     "prompt": "What is {n}³?", "vars": {"n": [2, 12]}, "formula": "n**3", "whole": True, "slips": ["3*n", "n*n", "n**3+n"],
     "steps": ["{n}³ means {n} × {n} × {n}.", "Work out {n} × {n} first, then multiply by {n} again."],
     "worked": "{n}³ = {n} × {n} × {n} = {n*n} × {n} = {answer}."},
    {"when": r"\bcube roots?\b|\bcubic numbers?|\bperfect cubes?", "unit": "cm", "level": "apply",
     "prompt": "A cube-shaped box has a volume of {v} cm³. How long is each edge?", "vars": {"n": [2, 12]},
     "derived": {"v": "n**3"}, "formula": "n", "whole": True, "slips": ["v/3", "v/6", "n*n"],
     "steps": ["Volume of a cube = edge × edge × edge, so the edge is the cube root of the volume.",
               "Which number multiplied by itself three times gives {v}?"],
     "worked": "∛{v} = {answer}, since {answer} × {answer} × {answer} = {v}. Each edge is {answer} cm."},
    {"when": r"\bexponent|\bpowers?\b|\bindex notation|\bpower play", "unit": "", "level": "apply",
     "prompt": "What is the value of {a}{b:sup}?", "vars": {"a": [2, 9], "b": [3, 5]}, "formula": "a**b", "whole": True,
     "slips": ["a*b", "b**a", "a**(b-1)"],
     "steps": ["{a}{b:sup} means {a} multiplied by itself {b} times.", "Multiply step by step: {a} × {a} = {a*a}, and keep going."],
     "worked": "{a}{b:sup} = {answer}."},
    {"when": r"\bexponent|\blaws? of (exponents|indices)|\bpowers?\b.*\bmultipl", "unit": "", "level": "apply",
     "prompt": "{a}{m:sup} × {a}{n:sup} = {a}ˣ. What is x?", "vars": {"a": [2, 9], "m": [2, 9], "n": [2, 9]},
     "formula": "m+n", "whole": True, "slips": ["m*n", "m-n", "a*(m+n)"],
     "steps": ["When the same number is multiplied, the powers are added: aᵐ × aⁿ = aᵐ⁺ⁿ."],
     "worked": "{a}{m:sup} × {a}{n:sup} = {a} to the power {m} + {n}, so x = {answer}."},
    {"when": r"\bpowers? of (10|ten)\b|\bscientific notation|\bstandard form", "unit": "", "level": "apply",
     "prompt": "Write {k} × 10{n:sup} as an ordinary number.", "vars": {"k": [2, 9], "n": [2, 6]}, "formula": "k*10**n",
     "whole": True, "slips": ["k*10**(n-1)", "k*10**(n+1)", "k*10*n"],
     "steps": ["10{n:sup} is 1 followed by {n} zeros.", "Multiply {k} by that."],
     "worked": "10{n:sup} = {10**n}, so {k} × 10{n:sup} = {answer}."},
    {"when": r"\bquadrilateral|\bangle sum|\bangles? of a (quadrilateral|four)", "unit": "°", "level": "apply",
     "prompt": "Three angles of a quadrilateral are {a}°, {b}° and {c}°. What is the fourth angle?",
     "vars": {"a": [60, 110, 5], "b": [60, 110, 5], "c": [60, 110, 5]}, "formula": "360-a-b-c", "whole": True,
     "slips": ["180-a-b-c+180-180", "a+b+c-180", "180-(a+b+c)/3"],
     "steps": ["The angles of a quadrilateral add up to 360°.", "Add the three angles, then take the total from 360°."],
     "worked": "{a}° + {b}° + {c}° = {a+b+c}°, so the fourth angle is 360° − {a+b+c}° = {answer}°."},
    {"when": r"\bdivisib|\bmultiple of\b|\bremainder|\bdigit sum|\bdigits in disguise", "unit": "", "level": "apply",
     "prompt": "What is the remainder when {n} is divided by 9? (Hint: use the sum of its digits.)", "vars": {"n": [1000, 99999]},
     "formula": "n%9", "whole": True, "slips": ["n%10", "(n%9)+1", "9-(n%9)"],
     "steps": ["A number leaves the same remainder on division by 9 as the sum of its digits does.",
               "Add the digits of {n}; keep adding until you have one digit (9 means remainder 0)."],
     "worked": "{n} ÷ 9 leaves remainder {answer}."},
    {"when": r"\bdistribut|\bproperties of multiplication", "unit": "", "level": "apply",
     "prompt": "Work out {a} × {b} + {a} × {c} quickly.", "vars": {"a": [12, 48], "b": [11, 60], "c": [11, 60]},
     "derived": {"d": "b+c"}, "formula": "a*(b+c)", "whole": True, "slips": ["a*b+c", "a+b+c", "a*b*c"],
     "steps": ["The distributive property: a × b + a × c = a × (b + c).", "Add {b} + {c} first, then multiply by {a}."],
     "worked": "{a} × {b} + {a} × {c} = {a} × ({b} + {c}) = {a} × {d} = {answer}."},
    {"when": r"\bdistribut|\bidentit|\bsquare of (the )?(sum|difference)|\bdifference of (two )?squares", "unit": "", "level": "apply",
     "prompt": "Use (a + b)(a − b) = a² − b² to work out {x}² − {y}².", "vars": {"x": [21, 99], "y": [1, 19]},
     "formula": "(x+y)*(x-y)", "whole": True, "slips": ["(x-y)**2", "x*x+y*y", "x-y"],
     "steps": ["{x}² − {y}² = ({x} + {y}) × ({x} − {y}).", "Work out {x+y} × {x-y}."],
     "worked": "{x}² − {y}² = ({x} + {y})({x} − {y}) = {x+y} × {x-y} = {answer}."},
    {"when": r"\bratios?\b|\bsharing|\bdivid\w* .{0,20}ratio|\bproportional", "unit": "₹", "level": "apply",
     "prompt": "₹{t} is shared between Asha and Vikram in the ratio {p} : {q}. How much does Asha get?",
     "vars": {"p": [1, 7], "q": [1, 7], "k": [20, 200, 10]}, "derived": {"t": "k*(p+q)"}, "formula": "k*p", "whole": True,
     "differ": ["p", "q"],
     "slips": ["t/2", "t*p/q", "t/p"],
     "steps": ["There are {p} + {q} = {p+q} equal parts.", "One part is ₹{t} ÷ {p+q}; Asha gets {p} parts."],
     "worked": "One part = ₹{t} ÷ {p+q} = ₹{k}; Asha gets {p} × ₹{k} = ₹{answer}."},
    {"when": r"\bproportion|\bunit price|\brule of three|\bproportional reasoning", "unit": "₹", "level": "apply",
     "prompt": "{a} notebooks cost ₹{c}. At the same price, how much do {b} notebooks cost?",
     "vars": {"a": [2, 9], "u": [12, 60], "b": [3, 20]}, "derived": {"c": "a*u"}, "formula": "b*u", "whole": True,
     "differ": ["a", "b"],
     "slips": ["c*a/b", "c+b-a", "c*b"],
     "steps": ["Find the cost of one notebook: ₹{c} ÷ {a}.", "Multiply that by {b}."],
     "worked": "One notebook costs ₹{c} ÷ {a} = ₹{u}, so {b} notebooks cost {b} × ₹{u} = ₹{answer}."},
    {"when": r"\binverse(ly)? proportion", "unit": "", "level": "apply",
     "prompt": "{w} workers can finish a job in {d} days. Working at the same rate, how many days will {v} workers take?",
     "vars": {"w": {"from": [4, 6, 8, 12]}, "d": {"from": [6, 9, 12, 15, 18]}, "v": {"from": [2, 3, 4, 6, 9, 12]}},
     "differ": ["w", "v"],
     "formula": "w*d/v", "whole": True, "slips": ["d*v/w", "d+w-v", "d"],
     "steps": ["More workers take fewer days: workers × days stays the same.", "Work out {w} × {d}, then divide by {v}."],
     "worked": "{w} × {d} = {w*d} worker-days; {w*d} ÷ {v} = {answer} days."},
    {"when": r"\bratios? in maps?\b|\bmaps?\b|\bscale drawing|\bscale\b.*\bkm", "unit": "km", "level": "apply",
     "prompt": "On a map, 1 cm stands for {s} km. Two towns are {d} cm apart on the map. How far apart are they?",
     "vars": {"s": [5, 50, 5], "d": [2, 15]}, "formula": "s*d", "whole": True, "slips": ["s+d", "s/d", "s*d/10"],
     "steps": ["Every 1 cm on the map is {s} km on the ground.", "Multiply {d} by {s}."],
     "worked": "{d} cm × {s} km per cm = {answer} km."},
    {"when": r"\bunit conversions?|\bconvert", "unit": "cm", "level": "apply",
     "prompt": "A ribbon is {m} m long. How long is it in centimetres?", "vars": {"m": [1.5, 9.5, 0.5]}, "formula": "m*100",
     "slips": ["m*10", "m*1000", "m/100"], "whole": True,
     "steps": ["1 m = 100 cm.", "Multiply {m} by 100."], "worked": "{m} m × 100 = {answer} cm."},
    {"when": r"\bpercent|\bdiscount|\bfractions in disguise", "unit": "₹", "level": "apply",
     "prompt": "A school bag costs ₹{p}. In a sale it is {d}% off. What is the sale price?",
     "vars": {"p": [200, 2000, 50], "d": [10, 50, 5]}, "formula": "p-p*d/100", "slips": ["p*d/100", "p-d", "p+p*d/100"],
     "steps": ["{d}% of ₹{p} is the discount: {d} ÷ 100 × {p}.", "Take the discount away from ₹{p}."],
     "worked": "Discount = {d}% of ₹{p} = ₹{p*d/100}; sale price = ₹{p} − ₹{p*d/100} = ₹{answer}."},
    {"when": r"\bpercent|\bfractions? as percent|\bfractions in disguise", "unit": "%", "level": "apply",
     "prompt": "{a} out of {b} students in a class wear glasses. What percentage is that?",
     "vars": {"b": {"from": [20, 25, 40, 50]}, "a": [1, 19]}, "formula": "a*100/b", "slips": ["a", "a*10/b", "b-a"],
     "steps": ["Percentage = part ÷ whole × 100.", "Work out {a} ÷ {b}, then multiply by 100."],
     "worked": "{a} ÷ {b} × 100 = {answer}%."},
    {"when": r"\bparallelogram", "unit": "cm²", "level": "apply",
     "prompt": "A parallelogram has a base of {b} cm and a height of {h} cm. What is its area?",
     "vars": {"b": [5, 20], "h": [3, 15]}, "formula": "b*h", "whole": True, "slips": ["b*h/2", "2*(b+h)", "b+h"],
     "steps": ["Area of a parallelogram = base × height.", "Multiply {b} by {h}."], "worked": "Area = {b} × {h} = {answer} cm²."},
    {"when": r"\btrapezi", "unit": "cm²", "level": "apply",
     "prompt": "A trapezium has parallel sides of {a} cm and {b} cm, and a height of {h} cm. What is its area?",
     "vars": {"a": [4, 16], "b": [6, 20], "h": {"from": [2, 4, 6, 8, 10]}}, "formula": "(a+b)*h/2", "slips": ["(a+b)*h", "a*b*h/2", "a*h"],
     "steps": ["Area of a trapezium = ½ × (sum of the parallel sides) × height.", "Add {a} + {b}, multiply by {h}, then halve it."],
     "worked": "Area = ½ × ({a} + {b}) × {h} = ½ × {a+b} × {h} = {answer} cm²."},
    {"when": r"\brhomb", "unit": "cm²", "level": "apply",
     "prompt": "The diagonals of a rhombus are {p} cm and {q} cm long. What is its area?",
     "vars": {"p": {"from": [6, 8, 10, 12, 14, 16]}, "q": [5, 20]}, "formula": "p*q/2", "slips": ["p*q", "p+q", "2*(p+q)"],
     "steps": ["Area of a rhombus = ½ × product of the diagonals.", "Multiply {p} by {q}, then halve it."],
     "worked": "Area = ½ × {p} × {q} = {answer} cm²."},
    {"when": r"\bthink of a number|\balgebra play|\bnumber tricks?", "unit": "", "level": "analyse",
     "prompt": "Think of a number. Double it, add {a}, halve the result, then take away the number you first thought of. "
               "What number do you always end with?", "vars": {"a": {"from": [4, 6, 8, 10, 12, 14, 16, 18, 20]}},
     "formula": "a/2", "whole": True, "slips": ["a", "a*2", "a/2+1"],
     "steps": ["Call the number x and follow the steps with x.", "Double: 2x; add {a}: 2x + {a}; now halve that, and take away x."],
     "worked": "x → 2x → 2x + {a} → x + {a/2} → {a/2}. You always end with {answer}."},
    {"when": r"\bmean\b|\baverage\b|\bbalancing act", "unit": "", "level": "apply",
     "prompt": "The mean of four numbers is {m}. Three of them are {a}, {b} and {c}. What is the fourth?",
     "vars": {"m": [20, 60], "a": [10, 70], "b": [10, 70], "c": [10, 70]}, "formula": "4*m-a-b-c", "whole": True, "positive": True,
     "slips": ["m", "3*m-a-b-c", "(a+b+c)/3"],
     "steps": ["The four numbers add up to 4 × mean = {4*m}.", "Take {a}, {b} and {c} away from {4*m}."],
     "worked": "Total = 4 × {m} = {4*m}; fourth = {4*m} − {a} − {b} − {c} = {answer}."},
]
for _t in LIBRARY:  # 'unit_' marks a unit to show in the answer but not ask about
    if "unit_" in _t:
        _t["unit"] = _t.pop("unit_")
