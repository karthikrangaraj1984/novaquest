"""PDF output: login slips (ST-2), parent progress report (SX-6) and dashboard exports (DB-4)."""
from __future__ import annotations

import io
import re
from typing import Any, Sequence
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#151A2E")
BLUE = colors.HexColor("#2B4BD8")
MUTED = colors.HexColor("#5A6178")
LINE = colors.HexColor("#C9CFE0")


def login_slips(school_code: str, class_label: str, students: Sequence[dict]) -> bytes:
    """8 cut-out login slips per A4 page: name, school code, class, roll and PIN (ST-2)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"NovaQuest login slips {class_label}")
    w, h = A4
    sw, sh = (w - 30 * mm) / 2, (h - 30 * mm) / 4
    for i, s in enumerate(students):
        if i and i % 8 == 0:
            c.showPage()
        col, row = (i % 8) % 2, (i % 8) // 2
        x, y = 15 * mm + col * sw, h - 15 * mm - (row + 1) * sh
        c.setDash(4, 3)
        c.setStrokeColor(LINE)
        c.roundRect(x + 3 * mm, y + 3 * mm, sw - 6 * mm, sh - 6 * mm, 4 * mm)
        c.setDash()
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x + 9 * mm, y + sh - 16 * mm, "NovaQuest")
        c.setFont("Helvetica", 11)
        c.drawString(x + 9 * mm, y + sh - 23 * mm, s["name"][:34])
        rows = [("School code", school_code), ("Class · Roll", f"{class_label} · {s['roll_no']}"), ("PIN", " ".join(s["pin"]))]
        for j, (k, v) in enumerate(rows):
            yy = y + sh - 33 * mm - j * 7 * mm
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 10)
            c.drawString(x + 9 * mm, yy, k)
            c.setFillColor(INK)
            c.setFont("Courier-Bold", 12)
            c.drawString(x + 38 * mm, yy, v)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawString(x + 9 * mm, y + 8 * mm, "Keep your PIN secret. Ask your teacher if you forget it.")
    c.save()
    return buf.getvalue()


def _doc(title: str) -> tuple[io.BytesIO, SimpleDocTemplate, dict]:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=title, author="NovaQuest")
    ss = getSampleStyleSheet()
    styles = {"h1": ParagraphStyle("h1", parent=ss["Heading1"], textColor=INK, fontSize=18, spaceAfter=4),
              "h2": ParagraphStyle("h2", parent=ss["Heading2"], textColor=INK, fontSize=12, spaceBefore=8, spaceAfter=4),
              "p": ParagraphStyle("p", parent=ss["BodyText"], textColor=INK, fontSize=10, leading=14),
              "muted": ParagraphStyle("m", parent=ss["BodyText"], textColor=MUTED, fontSize=9, leading=12)}
    return buf, doc, styles


def _table(rows: list[list[Any]], widths: list[float] | None = None) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                           ("TEXTCOLOR", (0, 0), (-1, -1), INK), ("LINEBELOW", (0, 0), (-1, 0), 0.8, LINE),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def parent_report(student: dict, overview: dict, institution: str) -> bytes:
    """One-page progress report a parent can read without an account (SX-6)."""
    buf, doc, st = _doc(f"Progress report: {student['name']}")
    story = [Paragraph(f"{student['name']}: learning progress", st["h1"]),
             Paragraph(f"{institution} · Class {student['class_label']} · NovaQuest", st["muted"]),
             Paragraph("These are learning-progress indicators from NovaQuest, not exam marks.", st["muted"]), Spacer(1, 6)]
    story.append(Paragraph("Chapters", st["h2"]))
    rows = [["Chapter", "Mastered", "Practised", "Learning"]] + [
        [f"{c['no']}. {c['title']}", str(c["mastered"]), str(c["practised"]), str(c["learning"])] for c in overview["chapters"]]
    story.append(_table(rows if len(rows) > 1 else rows + [["No activity yet", "", "", ""]], [90 * mm, 25 * mm, 25 * mm, 25 * mm]))
    story.append(Paragraph("Topics taught to NOVA, the AI classmate", st["h2"]))
    if overview["taught"]:
        for t in overview["taught"][:6]:
            story.append(Paragraph(f"• {t['title']}: {t['summary'] or ''}", st["p"]))
    else:
        story.append(Paragraph("None yet.", st["p"]))
    story.append(Paragraph("Recent results", st["h2"]))
    rr = [["Assignment", "Result"]] + [[r["title"], r.get("result_txt", "—")] for r in overview.get("released_scores", [])]
    story.append(_table(rr if len(rr) > 1 else rr + [["No released results yet", ""]], [120 * mm, 45 * mm]))
    notes = [r for r in overview.get("released_scores", []) if r.get("note")]
    if notes:
        story.append(Paragraph("Notes from teachers", st["h2"]))
        for r in notes:
            story.append(Paragraph(f"• {_esc(r['title'])}, {_esc(r.get('teacher') or '')}: {_esc(r['note'])}", st["p"]))
    if overview["plan"]:
        story.append(Paragraph("Suggested revision", st["h2"]))
        for p in overview["plan"]:
            story.append(Paragraph(f"• {_esc(p['title'])} ({_esc(p['chapter'])})" + (f": {_esc(p['why'])}" if p.get('why') else ""), st["p"]))
    doc.build(story)
    return buf.getvalue()


def table_report(title: str, subtitle: str, header: list[str], rows: list[list[Any]]) -> bytes:
    """A simple titled table, used for dashboard PDF exports that match the screen (DB-4)."""
    buf, doc, st = _doc(title)
    story = [Paragraph(title, st["h1"]), Paragraph(subtitle, st["muted"]), Spacer(1, 6),
             _table([header] + [[("" if v is None else str(v)) for v in r] for r in rows])]
    doc.build(story)
    return buf.getvalue()


def monthly_report(data: dict[str, Any]) -> bytes:
    """One page for a management meeting (DB-10): the month's activity per class, plan status, work set, AI skills
    and what needs attention. Same honesty rules as the screen: thin data says so."""
    d, month = data["d"], data["month"]
    buf, doc, st = _doc(f"{data['school']}: NovaQuest, {month:%B %Y}")
    k = d["kpis"]
    active_month = round(sum(c["month_active_pct"] * c["students"] for c in d["classes"]) / data["students"]) if data["students"] else 0
    story = [Paragraph(f"{_esc(data['school'])}: NovaQuest report, {month:%B %Y}", st["h1"]),
             Paragraph("Learning-progress indicators, not exam marks. A figure needs at least "
                       f"{d['thresholds']['min']} students behind it.", st["muted"]), Spacer(1, 6),
             Paragraph(f"<b>{data['students']}</b> students · <b>{active_month}%</b> active this month · "
                       f"<b>{data['assignments']}</b> assignments set · <b>{data['escaped']}</b> escape rooms escaped · "
                       + (f"<b>{k['on_time']}%</b> handed in on time (last 8 weeks)" if k["on_time"] is not None
                          else "no assignments closed yet"), st["p"])]
    story.append(Paragraph("Classes", st["h2"]))
    rows = [["Class", "Teacher", "Active", "Understanding", "Set", "On time", "Term plan"]]
    for c in d["classes"]:
        plan = "No plan" if not c["plan"]["planned"] else (f"{c['plan']['behind']} behind" if c["plan"]["behind"] else "On track")
        rows.append([c["label"], c["teachers"], f"{c['month_active_pct']}%",
                     f"{c['month_understanding']}%" if c["month_understanding"] is not None else "Not enough data",
                     str(c["month_assignments"]), f"{c['on_time']}%" if c["on_time"] is not None else "None closed", plan])
    story.append(_table(rows, [16 * mm, 32 * mm, 16 * mm, 30 * mm, 12 * mm, 22 * mm, 30 * mm]))
    story.append(Paragraph("Teachers", st["h2"]))
    trows = [["Teacher", "Classes", "Assignments set", "Last set"]]
    for t in d["teachers"]:
        trows.append([t["name"], t["classes"] or "—", str(t["assignments"]), t["last_set"].strftime("%d %b") if t["last_set"] else "Never"])
    story.append(_table(trows, [45 * mm, 45 * mm, 35 * mm, 35 * mm]))
    story.append(Paragraph("AI skills (escape rooms)", st["h2"]))
    if d["ai_skills"]:
        story.append(_table([["Skill", "Average", "Students"]] + [[s["concept"], f"{s['pct']}%", str(s["students"])] for s in d["ai_skills"]],
                            [90 * mm, 30 * mm, 30 * mm]))
    else:
        story.append(Paragraph("Not enough data yet.", st["p"]))
    story.append(Paragraph("Needs attention", st["h2"]))
    for x in d["concerns"][:8] or [{"text": "Nothing: every class is active, on plan and handing in work."}]:
        story.append(Paragraph(f"• {_esc(x['text'])}", st["p"]))
    doc.build(story)
    return buf.getvalue()


# ---------- exam papers (AS-8) ----------
_SUPERS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿˣʸᵐⁱ⁽⁾"
_FROM_SUPER = str.maketrans(_SUPERS, "0123456789+-nxymi()")


def _safe(text: Any) -> str:
    """Text for a PDF paragraph: escaped, with the few symbols the standard fonts can't draw replaced."""
    s = _esc(str(text or ""))
    for a, b in (("→", "->"), ("←", "<-"), ("≥", ">="), ("≤", "<="), ("≈", "~"), ("−", "-"), ("…", "..."), ("‘", "'"), ("’", "'"),
                 ("√", "√"), ("∛", "cube root of "), ("π", "pi"), ("₹", "Rs "), ("∆", "triangle ")):
        s = s.replace(a, b)
    # powers (2⁵, 10⁻³, aᵐ⁺ⁿ, 4ˣ): drawn raised and small, since the standard PDF fonts have no superscript digits
    s = re.sub(f"[{_SUPERS}]+", lambda m: "<super>" + m.group(0).translate(_FROM_SUPER) + "</super>", s)
    return s.replace("√", "sqrt ")


class _NumberedCanvas(canvas.Canvas):
    """'Page 2 of 3' on every page, and the set's name, so loose pages can be put back in order."""

    def __init__(self, *args: Any, footer: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []
        self._footer = footer

    def showPage(self) -> None:  # noqa: N802 (reportlab's name)
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8)
            self.setFillColor(MUTED)
            self.drawString(18 * mm, 10 * mm, self._footer)
            self.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _marks_txt(m: float) -> str:
    whole, half = int(m), m - int(m) >= 0.5
    return (str(whole) if whole else "") + ("½" if half else "") or "0"


def exam_paper(v: dict[str, Any], set_no: int, school: str, *, key: bool = False) -> bytes:
    """The question paper (or its answer key and marking scheme) for one set, ready to print on A4."""
    p, s = v["paper"], v["sets"][set_no - 1]
    name = f"Set {s['name']}"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm, bottomMargin=18 * mm,
                            title=f"{p['title']} ({name}){' answer key' if key else ''}", author="NovaQuest")
    ss = getSampleStyleSheet()
    st = {"school": ParagraphStyle("sc", parent=ss["Title"], fontSize=14, leading=17, spaceAfter=0, textColor=INK),
          "title": ParagraphStyle("ti", parent=ss["Title"], fontSize=12.5, leading=16, spaceAfter=2, textColor=INK),
          "p": ParagraphStyle("p", parent=ss["BodyText"], fontSize=10.5, leading=14, textColor=INK),
          "small": ParagraphStyle("s", parent=ss["BodyText"], fontSize=9.5, leading=12.5, textColor=INK),
          "sec": ParagraphStyle("se", parent=ss["Heading2"], fontSize=11.5, leading=14, spaceBefore=10, spaceAfter=2, textColor=INK),
          "note": ParagraphStyle("n", parent=ss["BodyText"], fontSize=9.5, leading=12, textColor=MUTED, fontName="Helvetica-Oblique"),
          "right": ParagraphStyle("r", parent=ss["BodyText"], fontSize=10, alignment=2, textColor=INK)}
    total = v["total"]
    hours = p["duration_min"] / 60
    time_txt = (f"{p['duration_min']} minutes" if p["duration_min"] < 60 or p["duration_min"] % 30
                else (_marks_txt(hours) + " hour" + ("s" if hours != 1 else "")))
    story: list[Any] = [Paragraph(_safe(school), st["school"]),
                        Paragraph(_safe(p["title"]) + (" — Answer key and marking scheme" if key else ""), st["title"])]
    head = [[Paragraph(f"Class {_safe(p['class_label'])} · {_safe(p['subject'])}", st["p"]), Paragraph(f"<b>{name}</b>", st["right"])],
            [Paragraph(f"Time allowed: {time_txt}", st["p"]), Paragraph(f"Maximum marks: {_marks_txt(total)}", st["right"])]]
    ht = Table(head, colWidths=[110 * mm, 64 * mm])
    ht.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("LINEBELOW", (0, -1), (-1, -1), 0.8, INK)]))
    story.append(ht)
    if not key:
        who = Table([[Paragraph("Name: ______________________________", st["p"]), Paragraph("Roll no.: ________", st["p"]),
                      Paragraph("Date: ____________", st["p"])]], colWidths=[84 * mm, 45 * mm, 45 * mm])
        who.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story.append(who)
        lines = ["All questions are compulsory.",
                 f"This question paper has {v['questions']} questions in {len(s['sections'])} section{'s' if len(s['sections']) != 1 else ''}."]
        for sec in s["sections"]:
            how = {"mcq": "multiple-choice questions; write the letter of the correct option",
                   "short": "short-answer questions", "long": "long-answer questions in two parts",
                   "numeric": "problems; show your working"}[sec["kind"]]
            lines.append(f"Section {sec['name']} has {sec['count']} {how}, {_marks_txt(sec['marks'])} mark"
                         f"{'s' if sec['marks'] != 1 else ''} each.")
        lines.append("Marks for each question are shown on the right.")
        if not p["answer_space"]:
            lines.append("Write your answers in the answer booklet.")
        lines += [x.strip() for x in (p["instructions"] or "").splitlines() if x.strip()]
        story.append(Paragraph("<b>General instructions</b>", st["small"]))
        story += [Paragraph(f"{i}. {_safe(t)}", st["small"]) for i, t in enumerate(lines, 1)]
    for sec in s["sections"]:
        story.append(Paragraph(f"SECTION {sec['name']}"
                               f"<font size=10> — {sec['count']} × {_marks_txt(sec['marks'])} = {_marks_txt(sec['total'])} marks</font>",
                               st["sec"]))
        for it in sec["items"]:
            story.append(_exam_item(it, st, key=key, space=bool(p["answer_space"]) and not key))
    story.append(Spacer(1, 10))
    story.append(Paragraph("— End of the answer key —" if key else "— End of the question paper —",
                           ParagraphStyle("end", parent=st["note"], alignment=1)))
    footer = f"{p['title']} · Class {p['class_label']} · {name}{' · answer key' if key else ''}"
    doc.build(story, canvasmaker=lambda *a, **kw: _NumberedCanvas(*a, footer=footer, **kw))
    return buf.getvalue()


def _exam_item(it: dict[str, Any], st: dict, *, key: bool, space: bool) -> Table:
    """One numbered question: text (with parts or options) on the left, its marks on the right."""
    body: list[Any] = []
    if it["parts"]:
        body.append(Paragraph(f"(a) {_safe(it['parts'][0])}", st["p"]))
        body.append(Paragraph(f"(b) {_safe(it['parts'][1])}", st["p"]))
    else:
        body.append(Paragraph(_safe(it["prompt"]), st["p"]))
    if it["options"] and not key:
        opts = [Paragraph(f"({'abcd'[i]}) {_safe(o)}", st["p"]) for i, o in enumerate(it["options"][:4])]
        if max(len(o) for o in it["options"]) <= 38:
            grid = Table([opts[0:2], opts[2:4]], colWidths=[72 * mm, 72 * mm])
        else:
            grid = Table([[o] for o in opts], colWidths=[144 * mm])
        grid.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        body.append(grid)
    if key:
        if it["letter"]:
            body.append(Paragraph(f"<b>Answer: ({it['letter']}) {_safe(it['right_text'])}</b>", st["small"]))
        else:
            if it["model"]:
                body.append(Paragraph(f"<b>Expected answer:</b> {_safe(it['model'])}", st["small"]))
            for point, m in it["scheme"]:
                body.append(Paragraph(f"• {_safe(point)} <b>[{_marks_txt(m)}]</b>", st["small"]))
    elif space and not it["options"]:
        rows = max(2, int(round(it["marks"] * 2)))
        lines = Table([[""] for _ in range(rows)], colWidths=[146 * mm], rowHeights=[7.5 * mm] * rows)
        lines.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]))
        body.append(lines)
    t = Table([[Paragraph(f"<b>{it['no']}.</b>", st["p"]), body, Paragraph(f"[{_marks_txt(it['marks'])}]", st["right"])]],
              colWidths=[9 * mm, 150 * mm, 15 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def unit_pack(v: dict[str, Any]) -> bytes:
    """A teacher's prep pack for one chapter (1.26.0): the lesson plan by period, mix-ups to watch for, the book's
    activities, key terms and the teacher's own notes."""
    ch, p = v["ch"], v["p"]
    part = f"Part {ch['part']} · " if ch.get("part") else ""
    title = f"{part}Chapter {ch['code']}: {ch['title']}"
    buf, doc, st = _doc(f"Prep pack: {title}")
    sub = f"{v['cur']['book']['label']} · class {v['cur']['class_label']} · {v['periods']} periods of {v['minutes']} minutes"
    if v.get("dates"):
        sub += f" · term plan {v['dates']['starts_on']:%d %b} – {v['dates']['ends_on']:%d %b}"
    story = [Paragraph(_safe(title), st["h1"]), Paragraph(_safe(sub), st["muted"]), Spacer(1, 4), Paragraph(_safe(p["overview"]), st["p"])]
    acts = v.get("acts", {})
    story.append(Paragraph("Lesson plan", st["h2"]))
    for lesson in v["lessons"]:
        story.append(Paragraph(f"<b>Period {lesson['no']}: {_safe(lesson['title'])}</b>", st["p"]))
        story.append(Paragraph("Flow: " + " → ".join(f"{_safe(w)} ({m} min)" for m, w in lesson["flow"]).replace("→", "-&gt;"), st["muted"]))
        for t in lesson["topics"] if lesson["kind"] == "teach" else []:
            if len(lesson["topics"]) > 1:
                story.append(Paragraph(f"<b>{_safe(t['code'])} {_safe(t['title'])}</b>" + (f" (textbook {_safe(t['pages'])})" if t["pages"] else ""), st["p"]))
            elif t["pages"]:
                story.append(Paragraph(f"Textbook {_safe(t['pages'])}", st["muted"]))
            story.append(Paragraph(f"Objective: {_safe(t['objective'])}", st["p"]))
            story.append(Paragraph(f"Hook: {_safe(t['hook'])}", st["p"]))
            for k in t["key_points"]:
                story.append(Paragraph(f"• {_safe(k)}", st["p"]))
            if t.get("activity"):
                a = acts.get(t["activity"])
                story.append(Paragraph("Activity: " + (f"Activity {_safe(t['activity'])}: {_safe(a['title'])}" if a else _safe(t["activity"])), st["p"]))
            if t.get("check") and not t["check"]["options"]:
                c = t["check"]
                story.append(Paragraph(f"Check: at the end, ask again: \u201c{_safe(c['q'])}\u201d"
                                       + (f" Listen for: {_safe(' '.join(c.get('listen') or []))}" if c.get("listen") else ""), st["p"]))
            elif t.get("check"):
                c = t["check"]
                opts = "   ".join(f"{'ABCDEFGH'[i]}. {_safe(o)}" for i, o in enumerate(c["options"]))
                story.append(Paragraph(f"Check: {_safe(c['q'])}<br/>{opts}<br/><i>Answer: {'ABCDEFGH'[c['answer']]}"
                                       + (f". {_safe(c['why'])}" if c.get("why") else "") + "</i>", st["p"]))
            if t.get("watch_for"):
                story.append(Paragraph(f"Watch for: {_safe('; '.join(t['watch_for']))}", st["p"]))
        if lesson["kind"] == "practice":
            story.append(Paragraph("Practise and apply: " + _safe(", ".join(t["title"] for t in lesson["topics"])) + ".", st["p"]))
        if lesson["kind"] == "activity":
            story.append(Paragraph(_safe("; ".join((f"Activity {a['ref']}: " if a.get("ref") else "") + a["title"]
                                                   for a in lesson.get("activities", []))), st["p"]))
        if lesson["kind"] == "review":
            story.append(Paragraph("Recall the class's first ideas, clear up the mix-ups below, then a quiz on the whole chapter.", st["p"]))
        story.append(Spacer(1, 4))
    story.append(Paragraph("Mix-ups to watch for", st["h2"]))
    for m in p["mixups"] or [{"label": "None drafted for this chapter yet.", "wrong": "", "right": "", "spot": "", "fix": ""}]:
        bits = [f"<b>{_safe(m['label'])}</b>"] + [f"{k}: {_safe(m[f])}" for k, f in (("A student might say", "wrong"), ("Right idea", "right"),
                                                                                    ("How you'll notice", "spot"), ("Clear it up", "fix")) if m.get(f)]
        story.append(Paragraph("<br/>".join(bits), st["p"]))
        story.append(Spacer(1, 3))
    story.append(Paragraph("Activities and demonstrations", st["h2"]))
    for a in p["activities"]:
        head = (f"Activity {a['ref']}: " if a.get("ref") else "Extra idea: ") + a["title"] + f" ({a['kind'].lower()})"
        bits = [f"<b>{_safe(head)}</b>"]
        if a.get("safety"):
            bits.append(f"<b>Safety:</b> {_safe(a['safety'])}")
        if a.get("materials"):
            bits.append("Get ready: " + _safe("; ".join(a["materials"])))
        bits += [f"{i}. {_safe(x)}" for i, x in enumerate(a.get("steps", []), 1)]
        if a.get("ask"):
            bits.append("Ask: " + _safe(" ".join(a["ask"])))
        story.append(Paragraph("<br/>".join(bits), st["p"]))
        story.append(Spacer(1, 3))
    if not p["activities"]:
        story.append(Paragraph("The book has no numbered activities in this chapter.", st["p"]))
    if p["terms"]:
        story.append(Paragraph("Key terms", st["h2"]))
        story.append(Paragraph(_safe(" · ".join(t["term"] + (f": {t['meaning']}" if t.get("meaning") else "") for t in p["terms"])), st["p"]))
    if v.get("notes"):
        story.append(Paragraph("My notes", st["h2"]))
        story.append(Paragraph(_safe(v["notes"]).replace("\n", "<br/>"), st["p"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Made with NovaQuest from your textbook" + (" by Claude" if p.get("written_by") == "claude" else "")
                           + ". Check activities and safety notes before class.", st["muted"]))
    doc.build(story)
    return buf.getvalue()


def privacy_notice(school: str, retention: dict[str, int]) -> bytes:
    """A parent notice about the child's data, with a consent form to sign and return (1.19.0)."""
    buf, doc, st = _doc(f"{school}: NovaQuest notice for parents")

    p = lambda t: Paragraph(_esc(t), st["p"])  # noqa: E731
    story = [Paragraph(_esc(f"{school}: how NovaQuest uses your child's information"), st["h1"]),
             Paragraph("A notice for parents and guardians, with a consent form on the last part of this page.", st["muted"]),
             Paragraph("What NovaQuest is", st["h2"]),
             p(f"NovaQuest is the learning app {school} uses for practice, assignments and lessons from your child's "
               "textbooks. It includes NOVA, an AI study partner: your child can ask NOVA for help, and can teach NOVA a "
               "topic to show they understand it."),
             Paragraph("What we keep", st["h2"]),
             p("Your child's name, class and roll number; your email address (for progress reports); their answers, scores "
               "and progress on each topic; and what they write to NOVA. If a teacher marks work done on paper, photos of "
               "those answer pages. We do not collect photos of your child, phone numbers, addresses or location, and "
               "there are no advertisements."),
             Paragraph("Who sees it", st["h2"]),
             p("Your child's teachers, the principal and the school's NovaQuest administrator. The principal can read "
               "children's conversations with NOVA, to keep them safe; each look is recorded. Nothing is sold, and "
               "nothing is shared with anyone else except as described in the next section."),
             Paragraph("AI and your child's information", st["h2"]),
             p("To mark written answers and to let NOVA reply, the text of the question and your child's answer is sent "
               "to Anthropic's Claude AI service; photos of answers written on paper are sent to it to be read. Your child's name is not sent, except their first name when a teacher "
               "asks NovaQuest to draft a note to them. Under Anthropic's terms for organisations using its service, this "
               "is not used to train Anthropic's AI. Multiple-choice "
               "marking, practice and the Daily 5 review work without AI."),
             p("If your child speaks to NOVA with the microphone button, the phone or browser turns their voice into text "
               "(in Chrome, using Google's speech service). NovaQuest receives only the text, and never records or keeps "
               "audio."),
             Paragraph("How long we keep it", st["h2"]),
             p(f"Conversations with NOVA and photos of paper answers are deleted after {retention['ai_chat_months']} months."
               if retention["ai_chat_months"] else
               "Conversations with NOVA and photos of paper answers are kept until the school deletes them."),
             p(f"When your child leaves the school, their information is erased {retention['left_student_months']} months "
               "after they leave. Marks are then kept only without a name, for the school's class figures."
               if retention["left_student_months"] else
               "When your child leaves the school, their information is kept until the school erases it. Marks are then "
               "kept only without a name, for the school's class figures."),
             Paragraph("Your rights", st["h2"]),
             p("You can ask the school, at any time, for a copy of everything NovaQuest holds about your child, to correct "
               "it, or to erase it. You can withdraw consent at any time; your child then can't use NovaQuest. Ask the "
               "class teacher or the school office."),
             Spacer(1, 10),
             Paragraph("Consent form", st["h2"]),
             p(f"I am the parent or guardian of the child named below. I have read this notice and agree that {school} "
               "may use NovaQuest for my child as described."),
             Spacer(1, 8),
             _table([["Child's name", ""], ["Class and roll number", ""], ["Parent / guardian name", ""],
                     ["Email (for progress reports)", ""], ["Signature and date", ""]], [62 * mm, 112 * mm])]
    doc.build(story)
    return buf.getvalue()
