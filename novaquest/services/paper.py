"""Marking work done on paper (1.21.0).

Many classes still write on paper (the printed worksheet, an exercise book). The teacher photographs each student's
answers (or scans them to a PDF) and uploads them on the assignment's "Paper work" page. With Claude, the handwriting
is read in the background: each question's answer is typed out exactly as written (not corrected), with anything hard
to read marked "unclear". Without Claude, the teacher types the answers in. Either way the teacher checks the reading
next to the photos, then "Mark" marks it exactly like work handed in online (multiple choice and number problems
exactly; written answers against their marking points), and it goes through the usual review and release.

The photos are kept in the data folder (paper/<school>/<assignment>/<student>/), never in the release, and are deleted
with the school's AI-conversation retention period, or when the student is erased."""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO

from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import assignments, jobs, maths
from novaquest.services.access import Actor, require, require_class
from novaquest.services.questions import assignment_questions

JOB = "paper_read"
MAX_PAGES = 6
MAX_IMAGE = 5 * 1024 * 1024    # Claude reads images up to 5 MB each
MAX_PDF = 10 * 1024 * 1024
LETTERS = "ABCDEFGH"
_TYPES = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def storage_dir(data_dir: Path) -> Path:
    return data_dir / "paper"


def _folder(root: Path, inst: int, assignment_id: int, student_id: int) -> Path:
    return root / str(inst) / str(assignment_id) / str(student_id)


def kind_of(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"%PDF-":
        return "pdf"
    return None


def _pdf_pages(data: bytes, out: Path, start: int) -> list[Path]:
    """A scanned PDF's pages as JPEG pictures (Poppler's pdftoppm)."""
    exe = shutil.which("pdftoppm")
    if not exe:
        raise ValidationError("This computer can't open PDF scans (Poppler isn't installed). Upload photos (JPEG or PNG) instead.")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "scan.pdf"
        src.write_bytes(data)
        try:
            subprocess.run([exe, "-jpeg", "-r", "130", "-l", str(MAX_PAGES), str(src), str(Path(tmp) / "p")],
                           check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValidationError("That PDF couldn't be opened. Try photos (JPEG or PNG) instead.")
        pages = sorted(Path(tmp).glob("p*.jpg"))
        saved = []
        for i, p in enumerate(pages):
            dest = out / f"page-{start + i + 1}.jpeg"
            if p.stat().st_size > MAX_IMAGE:
                raise ValidationError("A page of that PDF is too large to read. Scan at a lower resolution, or use photos.")
            shutil.move(str(p), dest)
            saved.append(dest)
    return saved


def pages(root: Path, inst: int, assignment_id: int, student_id: int) -> list[Path]:
    folder = _folder(root, inst, assignment_id, student_id)
    return sorted(folder.glob("page-*.*"), key=lambda p: int(p.stem.split("-")[1])) if folder.exists() else []


def _questions(tx: Tx, assignment_id: int, student_id: int) -> list[dict]:
    """The assignment's questions as this student saw them (their own numbers), numbered as on the worksheet."""
    out = []
    for q in assignment_questions(tx, assignment_id):
        if q["status"] != "approved" or q["kind"] in ("learn", "teach", "escape"):
            continue
        q = maths.for_student(q, student_id)
        out.append({**q, "no": len(out) + 1})
    return out


def overview(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """Every student the assignment is for, with how their work stands: online, paper (and its stage) or nothing."""
    a = assignments.get(tx, assignment_id)
    require_class(tx, actor, a["class_id"])
    online = {r["student_id"] for r in tx.all("""SELECT student_id FROM attempts WHERE assignment_id=%s AND status='submitted'
                                                 AND source='online'""", [assignment_id])}
    scans = {r["student_id"]: r for r in tx.all("SELECT * FROM paper_scans WHERE assignment_id=%s", [assignment_id])}
    rows = []
    for s in assignments.target_students(tx, assignment_id):
        sc = scans.get(s["id"])
        state = "online" if s["id"] in online else (sc["status"] if sc else "none")
        rows.append({**s, "scan": sc, "state": state})
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("online", "none", "reading", "to_check", "marked", "failed")}
    return {"assignment": a, "students": rows, "counts": counts, "questions": len(_questions(tx, assignment_id, 0))}


def upload(db: Db, actor: Actor, assignment_id: int, student_id: int, files: list[tuple[str, BinaryIO]], root: Path,
           *, claude: bool) -> int:
    """Store a student's photos (or a scanned PDF) and start reading them. Replaces earlier photos not yet marked."""
    require(actor, "teacher")
    with db.tenant(actor.institution_id) as tx:
        a = assignments.get(tx, assignment_id)
        require_class(tx, actor, a["class_id"], write=True)
        if a["state"] == "draft":
            raise Conflict("Schedule the assignment first: its questions aren't final yet.")
        if student_id not in {s["id"] for s in assignments.target_students(tx, assignment_id)}:
            raise NotFound("That student isn't set this assignment.")
        if tx.val("""SELECT 1 FROM attempts WHERE assignment_id=%s AND student_id=%s AND status='submitted'""",
                  [assignment_id, student_id]):
            raise Conflict("This student's work is already marked (handed in online or on paper).")
    datas = []
    for name, fh in files:
        data = fh.read(MAX_PDF + 1)
        if not data:
            continue
        kind = kind_of(data)
        if kind is None:
            raise ValidationError(f"“{name}” isn't a photo NovaQuest can read. Use JPEG or PNG (on iPhone: Settings → Camera → "
                                  "Formats → Most Compatible), or a PDF scan.")
        if (kind == "pdf" and len(data) > MAX_PDF) or (kind != "pdf" and len(data) > MAX_IMAGE):
            raise ValidationError(f"“{name}” is too large. Photos can be up to 5 MB and PDF scans up to 10 MB.")
        datas.append((kind, data))
    if not datas:
        raise ValidationError("Choose the photos of the student's answers.")
    folder = _folder(root, actor.institution_id, assignment_id, student_id)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for kind, data in datas:
        if kind == "pdf":
            saved += _pdf_pages(data, folder, len(saved))
        else:
            p = folder / f"page-{len(saved) + 1}.{kind}"
            p.write_bytes(data)
            saved.append(p)
    if len(saved) > MAX_PAGES:
        shutil.rmtree(folder)
        raise ValidationError(f"Up to {MAX_PAGES} pages per student.")
    now = clock.now()
    with db.tenant(actor.institution_id) as tx:
        sid = tx.val("""INSERT INTO paper_scans(institution_id, assignment_id, student_id, pages, status, reading, read_by,
                        uploaded_by, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,'{}',NULL,%s,%s,%s)
                        ON CONFLICT (assignment_id, student_id) DO UPDATE SET pages=EXCLUDED.pages, status=EXCLUDED.status,
                        reading='{}', read_by=NULL, error=NULL, uploaded_by=EXCLUDED.uploaded_by, updated_at=EXCLUDED.updated_at
                        RETURNING id""",
                     [tx.inst, assignment_id, student_id, len(saved), "reading" if claude else "to_check", actor.user_id, now, now])
        if claude:
            jobs.enqueue(tx, JOB, {"scan_id": sid}, dedupe_key=f"{JOB}:{sid}:{now.isoformat()}")
        audit.record(tx, "paper.upload", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                     detail={"student": student_id, "pages": len(saved)})
    return sid


def read(db: Db, engine: Any, inst: int, scan_id: int, root: Path) -> None:
    """Background job: Claude reads the handwriting into a draft for the teacher to check."""
    with db.tenant(inst) as tx:
        sc = tx.one("SELECT * FROM paper_scans WHERE id=%s", [scan_id])
        if not sc or sc["status"] != "reading":
            return
        qs = _questions(tx, sc["assignment_id"], sc["student_id"])
    images = []
    for p in pages(root, inst, sc["assignment_id"], sc["student_id"]):
        kind = p.suffix.lstrip(".")
        images.append((_TYPES.get(kind, "image/jpeg"), base64.b64encode(p.read_bytes()).decode()))
    items = [{"no": q["no"], "kind": "choice" if q["options"] else ("number" if q["kind"] == "numeric" else "written"),
              "question": q["prompt"], "options": [f"{LETTERS[i]}. {o}" for i, o in enumerate(q["options"] or [])]} for q in qs]
    reader = getattr(engine, "read_handwriting", None)
    got = reader(images, items) if reader and images else None
    with db.tenant(inst) as tx:
        if got is None:
            tx.exec("""UPDATE paper_scans SET status='to_check', read_by=NULL, error=%s, updated_at=%s WHERE id=%s""",
                    ["Claude couldn't read these photos. Type the answers from the photos below.", clock.now(), scan_id])
            return
        by_no = {q["no"]: q for q in qs}
        reading = {}
        for r in got:
            q = by_no.get(r.get("no"))
            if not q:
                continue
            entry: dict[str, Any] = {"unclear": bool(r.get("unclear"))}
            if q["options"]:
                letter = str(r.get("choice") or "").strip().upper()[:1]
                if letter and letter in LETTERS[:len(q["options"])]:
                    entry["index"] = LETTERS.index(letter)
            else:
                entry["text"] = str(r.get("text") or "")[:4000]
            reading[str(q["id"])] = entry
        tx.exec("""UPDATE paper_scans SET status='to_check', reading=%s, read_by='claude', error=NULL, updated_at=%s WHERE id=%s""",
                [jsonb(reading), clock.now(), scan_id])


def scan_view(tx: Tx, actor: Actor, scan_id: int) -> dict[str, Any]:
    sc = tx.one("""SELECT s.*, u.name, e.roll_no FROM paper_scans s JOIN users u ON u.id=s.student_id
                   LEFT JOIN enrolments e ON e.student_id=s.student_id AND e.status='active' WHERE s.id=%s""", [scan_id])
    if not sc:
        raise NotFound("Those photos weren't found.")
    a = assignments.get(tx, sc["assignment_id"])
    require_class(tx, actor, a["class_id"])
    qs = _questions(tx, sc["assignment_id"], sc["student_id"])
    for q in qs:
        q["read"] = (sc["reading"] or {}).get(str(q["id"]), {})
        q["letters"] = [(LETTERS[i], o) for i, o in enumerate(q["options"] or [])]
    return {"scan": sc, "assignment": a, "questions": qs}


def check_and_mark(db: Db, engine: Any, actor: Actor, scan_id: int, form: dict[str, str]) -> int:
    """Save the teacher's checked reading and mark it as the student's work. Returns the attempt id."""
    require(actor, "teacher")
    with db.tenant(actor.institution_id) as tx:
        view = scan_view(tx, actor, scan_id)
        sc, a = view["scan"], view["assignment"]
        require_class(tx, actor, a["class_id"], write=True)
        if sc["status"] == "marked":
            raise Conflict("This paper work is already marked.")
        if sc["status"] == "reading":
            raise Conflict("Claude is still reading these photos. Try again in a minute.")
        if tx.val("SELECT 1 FROM attempts WHERE assignment_id=%s AND student_id=%s AND status='submitted'",
                  [a["id"], sc["student_id"]]):
            raise Conflict("This student's work is already marked.")
        reading, unclear = {}, []
        for q in view["questions"]:
            key = f"q{q['id']}"
            if q["options"]:
                raw = str(form.get(key, "")).strip()
                entry = {"index": int(raw)} if raw.isdigit() and int(raw) < len(q["options"]) else {}
            else:
                entry = {"text": str(form.get(key, ""))[:4000]}
            reading[str(q["id"])] = {**entry, "unclear": bool(q["read"].get("unclear")) and not form.get(f"ok{q['id']}")}
            if reading[str(q["id"])]["unclear"]:
                unclear.append(q["id"])
        now = clock.now()
        no = int(tx.val("SELECT coalesce(max(attempt_no), 0) + 1 FROM attempts WHERE assignment_id=%s AND student_id=%s",
                        [a["id"], sc["student_id"]]))
        att = tx.val("""INSERT INTO attempts(institution_id, assignment_id, student_id, attempt_no, started_at, deadline_at, status,
                        source) VALUES (%s,%s,%s,%s,%s,%s,'in_progress','paper') RETURNING id""",
                     [tx.inst, a["id"], sc["student_id"], no, now, now + timedelta(minutes=1)])
        for qid, entry in reading.items():
            answer = {k: v for k, v in entry.items() if k in ("index", "text")}
            tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at) VALUES (%s,%s,%s,%s,%s)""",
                    [tx.inst, att, int(qid), jsonb(answer), now])
        tx.exec("""UPDATE paper_scans SET reading=%s, read_by=coalesce(read_by, 'teacher'), status='marked', attempt_id=%s,
                   updated_at=%s WHERE id=%s""", [jsonb(reading), att, now, scan_id])
    from novaquest.services import attempts
    attempts.submit(db, engine, None, att, institution_id=actor.institution_id)
    with db.tenant(actor.institution_id) as tx:
        if unclear:  # hard to read: the teacher sees these in "answers to check"
            tx.exec("UPDATE responses SET flagged=true WHERE attempt_id=%s AND question_id = ANY(%s)", [att, unclear])
        audit.record(tx, "paper.mark", actor_id=actor.user_id, target=f"attempt:{att}", detail={"scan": scan_id})
    return att


# ---------- clean-up ----------
def forget_student(tx: Tx, root: Path | None, student_id: int) -> None:
    """Erasure: the student's photos and readings go."""
    for r in tx.all("SELECT assignment_id FROM paper_scans WHERE student_id=%s", [student_id]):
        if root:
            shutil.rmtree(_folder(root, tx.inst, r["assignment_id"], student_id), ignore_errors=True)
    tx.exec("DELETE FROM paper_scans WHERE student_id=%s", [student_id])


def purge(tx: Tx, root: Path | None, before) -> int:
    """Retention: photos (and their readings) older than the school's period are deleted; marks stay."""
    old = tx.all("SELECT id, assignment_id, student_id FROM paper_scans WHERE created_at < %s", [before])
    for r in old:
        if root:
            shutil.rmtree(_folder(root, tx.inst, r["assignment_id"], r["student_id"]), ignore_errors=True)
    if old:
        tx.exec("DELETE FROM paper_scans WHERE id = ANY(%s)", [[r["id"] for r in old]])
    return len(old)
