"""A full demo school for trying NovaQuest's teacher, principal and admin screens with realistic data (1.30.0).

Green Valley Public School (demo), school code GVS-DEMO, academic year 2026-27:
  * classes 8A, 8B, 8C (Science and Mathematics) and 9A, 9B (Science), 18 students each, consent recorded;
  * Mrs Priya Rao teaches Science in 8A, 8B and 8C (a weekly timetable), Mr Arjun Iyer Mathematics in 8A and 8B
    (a timetable), Ms Fatima Sheikh Science in 9A and 9B and Mathematics in 8C (no timetable, to show both ways);
  * a principal and a school admin;
  * term plans from 15 June, eight weeks of practice and lessons, assignments released, closed, open and in draft
    (one set for three sections together), answers to check, and lessons presented on the projector.

Classes differ on purpose so the dashboards have something to say: 8A is strong and busy, 8C is behind its plan,
9B has gone quiet. Science 8 uses NovaQuest's ready-made NCERT book when it is installed, else a demo book.

Usage: python -m tools.demo_school [--password GreenValley2026]   (or: nq demo)"""
from __future__ import annotations

import argparse
import random
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from novaquest.ai.offline import OfflineEngine
from novaquest.core import clock, security
from novaquest.core.config import load_settings
from novaquest.core.db import Db, init_db, jsonb
from novaquest.services import assignments, books, institutions, mastery, plans, questions, students, syllabus, timetable
from novaquest.services.access import Actor
from tools import demo_book

CODE = "GVS-DEMO"
DOMAIN = "greenvalley.demo"
FIRST = ["Aadhya", "Aarav", "Diya", "Kavin", "Meera", "Nila", "Rohan", "Sanjana", "Tanvi", "Vikram", "Ishaan", "Zara", "Arjun",
         "Lakshmi", "Pranav", "Riya", "Siddharth", "Ananya", "Harini", "Karthik", "Neha", "Rahul", "Sneha", "Varun", "Divya",
         "Farhan", "Gauri", "Jai", "Kiran", "Mira", "Nikhil", "Pooja", "Reyansh", "Saanvi", "Tarun", "Uma", "Yash", "Aditi"]
LAST = ["Ramesh", "Sharma", "Krishnan", "Murugan", "Venkat", "Suresh", "Prasad", "Iyer", "Rao", "Nair", "Gupta", "Khan",
        "Menon", "Pillai", "Reddy", "Das", "Joshi", "Patel", "Bose", "Kumar", "Shetty", "Chandran", "Mehta", "Singh"]
# class -> (skill: typical score, activity: share of students who keep practising, days since the class was last active)
PROFILE = {"8A": (0.80, 0.9, 0), "8B": (0.68, 0.75, 0), "8C": (0.52, 0.4, 1), "9A": (0.74, 0.8, 0), "9B": (0.60, 0.45, 11)}
TERM_START = date(2026, 6, 15)


def _staff(db: Db, inst: int, name: str, email: str, roles: list[str], password: str, seen_days: int | None) -> Actor:
    with db.tenant(inst) as tx:
        uid = tx.val("""INSERT INTO users(institution_id, name, email, password_hash, last_login_at) VALUES (%s,%s,%s,%s,%s)
                        RETURNING id""", [inst, name, email, security.hash_secret(password),
                                         clock.now() - timedelta(days=seen_days) if seen_days is not None else None])
        for r in roles:
            tx.exec("INSERT INTO user_roles(user_id, institution_id, role) VALUES (%s,%s,%s)", [uid, inst, r])
    return Actor(uid, inst, roles[0], tuple(roles), name)


def _book(db: Db, admin: Actor, storage: Path, subject: str, grade: str, title: str, chapters: dict | None) -> int:
    """Upload, read and publish a demo book as the school's own book."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = demo_book.make(Path(tmp) / f"demo-{subject.lower()}-{grade}.pdf", chapters)
        with pdf.open("rb") as fh:
            sid = books.create(db, admin, board="CBSE", subject=subject, grade=grade, title=title, files=[(pdf.name, fh)], storage=storage)
    books.read_book(db, OfflineEngine(), admin.institution_id, sid, storage)
    wait_until_read(db, admin.institution_id, sid)
    with db.tenant(admin.institution_id) as tx:
        books.publish(tx, admin, sid)
    return sid


def wait_until_read(db: Db, inst: int, sid: int, seconds: float = 600) -> None:
    """With NovaQuest running, its worker may be the one reading the book: wait until it's read (or failed)."""
    import time as _time

    end = _time.monotonic() + seconds
    while True:
        with db.tenant(inst) as tx:
            row = tx.one("SELECT status, meta->>'error' AS error FROM syllabi WHERE id=%s", [sid])
        if row["status"] == "failed":
            raise SystemExit(f"NovaQuest couldn't read a demo book: {row['error'] or 'unknown reason'}")
        if row["status"] != "processing":
            return
        if _time.monotonic() > end:
            raise SystemExit("A demo book is taking too long to read. Check that NovaQuest is running (nq status) and try again.")
        _time.sleep(1)


def _names(rng: random.Random, n: int, used: set[str]) -> list[str]:
    out = []
    while len(out) < n:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name not in used:
            used.add(name)
            out.append(name)
    return out


def _at(d: date, rng: random.Random) -> datetime:
    """A moment on a school day, in IST, as UTC."""
    t = time(rng.randint(15, 20), rng.choice([0, 10, 20, 30, 40, 50]))
    return datetime.combine(d, t, tzinfo=clock.IST).astimezone(clock.now().tzinfo)


def _practice(tx, rng: random.Random, kids: list[dict], label: str, topics_by_chapter: list[tuple[dict, list[int]]],
              today: date) -> None:
    """Learning evidence for each student on the chapters taught so far: lessons and practice on the chapter's dates,
    scores around the class's typical level (each student a little stronger or weaker), then their mastery."""
    skill, activity, quiet = PROFILE[label]
    last_day = today - timedelta(days=quiet)
    for k in kids:
        own = min(0.98, max(0.25, rng.gauss(skill, 0.12)))
        keen = rng.random() < activity
        for ch, topic_ids in topics_by_chapter:
            if ch["starts_on"] > last_day:
                continue
            end = min(ch["ends_on"], last_day)
            for tid in topic_ids:
                if rng.random() > (0.95 if keen else 0.35):
                    continue
                days = max(1, (end - ch["starts_on"]).days + 1)
                n = rng.randint(1, 3 if keen else 1)
                for _ in range(n):
                    d = ch["starts_on"] + timedelta(days=rng.randrange(days))
                    kind = rng.choice(["lesson_started", "practice", "practice", "check"])
                    score = None if kind == "lesson_started" else round(min(1.0, max(0.0, rng.gauss(own, 0.15))), 3)
                    tx.exec("""INSERT INTO learn_events(institution_id, student_id, topic_id, kind, score, data, created_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""", [tx.inst, k["student_id"], tid, kind, score, jsonb({"demo": True}), _at(d, rng)])
                mastery.recompute(tx, k["student_id"], tid)


def _work(tx, rng: random.Random, teacher: Actor, cid: int, label: str, kids: list[dict], sid: int, qids: list[int],
          topic_ids: list[int], title: str, kind: str, opens: datetime, closes: datetime, *, released: bool,
          batch: int | None = None, flag: bool = False) -> int:
    """An assignment of a class with its questions, and (for work that has opened) answers from most students."""
    skill, activity, _ = PROFILE[label]
    state = "open" if closes > clock.now() else ("reviewed" if released else "closed")
    if opens > clock.now():
        state = "scheduled"
    aid = tx.val("""INSERT INTO assignments(institution_id, class_id, teacher_id, syllabus_id, title, kind, difficulty, topic_ids,
                       state, opens_at, expires_at, released_at, batch_id, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'medium',%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                 [tx.inst, cid, teacher.user_id, sid, title, kind, topic_ids, state, opens, closes,
                  closes + timedelta(days=1) if released else None, batch, opens - timedelta(hours=2)])
    tx.exec("INSERT INTO assignment_targets(assignment_id, institution_id, target_kind, target_id) VALUES (%s,%s,'class',%s)",
            [aid, tx.inst, cid])
    for i, q in enumerate(qids):
        tx.exec("INSERT INTO assignment_questions(assignment_id, institution_id, question_id, ord) VALUES (%s,%s,%s,%s)",
                [aid, tx.inst, q, i + 1])
    if opens > clock.now():
        return aid
    qs = {q["id"]: q for q in tx.all("SELECT id, kind, options, answer, topic_id FROM questions WHERE id = ANY(%s)", [qids])}
    window = closes if closes < clock.now() else clock.now()
    share = activity + (1 - activity) * 0.5 if closes < clock.now() else activity * 0.6
    for k in kids:
        if rng.random() > share:
            continue
        own = min(0.98, max(0.2, rng.gauss(skill, 0.14)))
        start = opens + (window - opens) * rng.uniform(0.05, 0.9)
        done = start + timedelta(minutes=rng.randint(8, 30))
        if done > window:
            done = window - timedelta(minutes=1)
        att = tx.val("""INSERT INTO attempts(institution_id, assignment_id, student_id, attempt_no, started_at, deadline_at, submitted_at, status)
                        VALUES (%s,%s,%s,1,%s,%s,%s,'submitted') RETURNING id""", [tx.inst, aid, k["student_id"], start, closes, done])
        per: dict[int, list[float]] = {}
        for qid in qids:
            q = qs[qid]
            right = rng.random() < own
            if q["options"]:
                correct = (q["answer"] or {}).get("index", 0)
                ans = {"index": correct if right else (correct + 1) % max(2, len(q["options"]))}
                score, conf = (1.0 if right else 0.0), 0.95
            else:
                ans = {"text": "Answer written by the student (demo)."}
                score, conf = (1.0 if right else rng.choice([0.0, 0.5])), (rng.choice([0.55, 0.9, 0.9]) if flag else 0.9)
            tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at, ai_score, max_score, ai_rationale,
                           confidence, flagged) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s)""",
                    [tx.inst, att, qid, jsonb(ans), done, score, "Marked against the textbook (demo).", conf, conf < 0.7])
            got = per.setdefault(q["topic_id"], [0.0, 0.0])
            got[0] += score
            got[1] += 1
        for tid, (got, mx) in per.items():
            tx.exec("""INSERT INTO learn_events(institution_id, student_id, topic_id, kind, score, data, created_at)
                       VALUES (%s,%s,%s,'assignment',%s,%s,%s)""",
                    [tx.inst, k["student_id"], tid, round(got / mx, 3), jsonb({"attempt_id": att, "key": f"attempt:{att}"}), done])
            mastery.recompute(tx, k["student_id"], tid)
    return aid


def _ncert_science_8(tx) -> int | None:
    """NovaQuest's ready-made NCERT Science 8 book if it is installed and current, else None (a demo book is used)."""
    return tx.val("""SELECT id FROM syllabi WHERE institution_id IS NULL AND board='NCERT' AND subject='Science' AND grade='8'
                     AND is_current AND status='published' ORDER BY id DESC LIMIT 1""")


def _retire(db: Db, inst: int) -> None:
    """A demo school that failed half-way: take it out of the way (its code and sign-ins renamed, made inactive) so
    the demo can simply be run again."""
    tag = clock.now().strftime("%Y%m%d%H%M%S")
    with db.platform() as tx:
        tx.exec("UPDATE institutions SET school_code=%s, status='inactive', name=name || ' (failed setup)' WHERE id=%s",
                [f"{CODE}-FAILED-{tag}", inst])
        tx.exec("UPDATE users SET email=email || %s, status='inactive' WHERE institution_id=%s AND email IS NOT NULL",
                [f".failed-{tag}", inst])


def seed(db: Db, password: str = "GreenValley2026", storage: Path | None = None, seed_no: int = 30) -> dict[str, Any]:
    """Create the demo school; returns the sign-in details. Refuses to run twice. If something goes wrong half-way,
    the half-made school is set aside so it can be run again."""
    if security.check_password_strength(password):
        raise SystemExit(security.check_password_strength(password))
    with db.platform() as tx:
        if tx.val("SELECT 1 FROM institutions WHERE school_code=%s", [CODE]):
            raise SystemExit(f"The demo school already exists (school code {CODE}). Its sign-ins are in var/demo-school-logins.txt.")
        inst = tx.val("""INSERT INTO institutions(name, kind, board, academic_year, school_code)
                         VALUES ('Green Valley Public School (demo)', 'school', 'CBSE', '2026-27', %s) RETURNING id""", [CODE])
        ncert = _ncert_science_8(tx)
    try:
        return _fill(db, inst, ncert, password, storage, random.Random(seed_no))
    except BaseException:
        _retire(db, inst)
        raise


def _fill(db: Db, inst: int, ncert: int | None, password: str, storage: Path | None, rng: random.Random) -> dict[str, Any]:
    now = clock.now()
    today = clock.to_ist(now).date()
    admin = _staff(db, inst, "Asha Menon", f"admin@{DOMAIN}", ["inst_admin"], password, 2)
    head = _staff(db, inst, "Dr Ramesh Kumar", f"principal@{DOMAIN}", ["leader"], password, 1)
    priya = _staff(db, inst, "Mrs Priya Rao", f"priya@{DOMAIN}", ["teacher"], password, 0)
    arjun = _staff(db, inst, "Mr Arjun Iyer", f"arjun@{DOMAIN}", ["teacher"], password, 1)
    fatima = _staff(db, inst, "Ms Fatima Sheikh", f"fatima@{DOMAIN}", ["teacher"], password, 16)
    storage = storage or books.storage_dir(load_settings().data_dir)
    sci8 = ncert or _book(db, admin, storage, "Science", "8", demo_book.TITLE, None)
    maths8 = _book(db, admin, storage, "Mathematics", "8", demo_book.MATHS_8_TITLE, demo_book.MATHS_8)
    sci9 = _book(db, admin, storage, "Science", "9", demo_book.SCIENCE_9_TITLE, demo_book.SCIENCE_9)
    teach = {("8A", sci8): priya, ("8B", sci8): priya, ("8C", sci8): priya, ("8A", maths8): arjun, ("8B", maths8): arjun,
             ("8C", maths8): fatima, ("9A", sci9): fatima, ("9B", sci9): fatima}
    used: set[str] = set()
    classes: dict[str, dict] = {}
    with db.tenant(inst) as tx:
        for label in PROFILE:
            cid = institutions.create_class(tx, admin, grade=label[0], section=label[1], academic_year="2026-27")
            institutions.set_class_subjects(tx, admin, cid, [sci8, maths8] if label[0] == "8" else [sci9])
            rows = [{"roll_no": str(i + 1), "name": n, "parent_email": f"parent.{label.lower()}{i + 1}@example.com"}
                    for i, n in enumerate(_names(rng, 18, used))]
            added = students.import_rows(tx, admin, cid, rows, skip_invalid=False).added
            for s in students.list_students(tx, cid):
                students.record_consent(tx, admin, s["enrolment_id"], parent_name="Parent (demo data)", method="signed_form")
            classes[label] = {"id": cid, "kids": added}
        for (label, sid), t in teach.items():
            institutions.assign_teacher(tx, admin, teacher_id=t.user_id, class_id=classes[label]["id"], syllabus_id=sid)
    # term plans: Science 8 across the year; the short demo books by the end of December
    with db.tenant(inst) as tx:
        for (label, sid), t in teach.items():
            cid = classes[label]["id"]
            end = date(2027, 2, 27) if sid == sci8 and ncert else date(2026, 12, 19)
            plans.autofill(tx, t, cid, sid, TERM_START, end)
    # timetables (Priya and Arjun; Fatima has none)
    with db.tenant(inst) as tx:
        for t, rows in ((priya, [("8A", sci8, [0, 1, 3, 4], time(9, 0)), ("8B", sci8, [0, 2, 3, 5], time(10, 40)),
                                 ("8C", sci8, [1, 2, 4], time(11, 50))]),
                        (arjun, [("8A", maths8, [0, 2, 3, 4], time(11, 50)), ("8B", maths8, [1, 2, 4, 5], time(9, 0))])):
            for label, sid, days, at in rows:
                timetable.add(tx, t, classes[label]["id"], sid, days, at, 40)
    # eight weeks of learning
    with db.tenant(inst) as tx:
        for (label, sid), t in teach.items():
            cid = classes[label]["id"]
            chs = tx.all("""SELECT p.chapter_id, p.starts_on, p.ends_on FROM term_plans p WHERE p.class_id=%s AND p.syllabus_id=%s
                            ORDER BY p.starts_on""", [cid, sid])
            tbc = [(ch, [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' ORDER BY ord",
                                                [ch["chapter_id"]])]) for ch in chs]
            _practice(tx, rng, classes[label]["kids"], label, tbc, today)
    # assignments: questions written once per book and chapter, then given to each class
    engine = OfflineEngine()
    bank: dict[tuple[int, int], list[int]] = {}

    def questions_for(t: Actor, cid: int, sid: int, chapter_id: int, kind: str) -> tuple[list[int], list[int]]:
        topic_ids = [r["id"] for r in db_topics(sid, chapter_id)]
        key = (chapter_id, kind == "quiz")
        if key not in bank:
            aid = questions.generate_assignment(db, engine, t, class_id=cid, topic_ids=topic_ids[:3], kind=kind, difficulty="medium",
                                                count=5, title="(demo question bank)")
            with db.tenant(inst) as tx:
                ids = [q["id"] for q in questions.assignment_questions(tx, aid) if q["status"] != "removed"]
                for q in ids:
                    questions.approve(tx, t, q)
                tx.exec("DELETE FROM assignments WHERE id=%s", [aid])
            bank[key] = ids
        return bank[key], topic_ids[:3]

    def db_topics(sid: int, chapter_id: int) -> list[dict]:
        with db.tenant(inst) as tx:
            return tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' ORDER BY ord", [chapter_id])

    def chapters(cid: int, sid: int) -> list[dict]:
        with db.tenant(inst) as tx:
            return tx.all("""SELECT p.chapter_id, p.starts_on, p.ends_on, c.code, c.title FROM term_plans p JOIN syllabus_nodes c
                             ON c.id=p.chapter_id WHERE p.class_id=%s AND p.syllabus_id=%s AND p.starts_on <= %s ORDER BY p.starts_on""",
                          [cid, sid, today])

    batch_ids: dict[str, int] = {}
    for (label, sid), t in teach.items():
        cid, kids = classes[label]["id"], classes[label]["kids"]
        done = chapters(cid, sid)
        if not done:
            continue
        recent = done[-3:]
        plan_rows = []
        if len(recent) >= 3:
            plan_rows.append((recent[0], "quiz", now - timedelta(days=24), now - timedelta(days=20), True, False))
        if len(recent) >= 2:
            plan_rows.append((recent[-2], "mission", now - timedelta(days=12), now - timedelta(days=8), True, False))
        plan_rows.append((recent[-2] if len(recent) >= 2 else recent[-1], "short", now - timedelta(days=4), now - timedelta(hours=20),
                          False, True))
        for ch, kind, opens, closes, released, flag in plan_rows:
            if label == "9B" and opens > now - timedelta(days=11):
                continue  # 9B has gone quiet: no work set lately
            qids, tids = questions_for(t, cid, sid, ch["chapter_id"], kind)
            with db.tenant(inst) as tx:
                _work(tx, rng, t, cid, label, kids, sid, qids, tids, f"{ch['title']}: {'quiz' if kind == 'quiz' else 'mission' if kind == 'mission' else 'written answers'}",
                      kind, opens, closes, released=released, flag=flag and label in ("8B", "9A"))
        # this week's work, open now; Science 8 set for the three sections together
        cur = done[-1]
        if label == "9B":
            continue
        qids, tids = questions_for(t, cid, sid, cur["chapter_id"], "quiz")
        with db.tenant(inst) as tx:
            title = f"{cur['title']}: quick quiz"
            batch = batch_ids.get(f"{sid}:{t.user_id}") if sid == sci8 else None
            aid = _work(tx, rng, t, cid, label, kids, sid, qids, tids, title, "quiz", now - timedelta(days=1, hours=3),
                        now + timedelta(days=2, hours=5), released=False, batch=batch)
            if sid == sci8 and batch is None:
                tx.exec("UPDATE assignments SET batch_id=id WHERE id=%s", [aid])
                batch_ids[f"{sid}:{t.user_id}"] = aid
    # a draft waiting for Priya, meant for all three sections
    with db.tenant(inst) as tx:
        sci_now = [r for r in tx.all("SELECT chapter_id, starts_on FROM term_plans WHERE class_id=%s AND syllabus_id=%s AND starts_on > %s ORDER BY starts_on LIMIT 1",
                                     [classes["8A"]["id"], sci8, today])]
    nxt = sci_now[0]["chapter_id"] if sci_now else chapters(classes["8A"]["id"], sci8)[-1]["chapter_id"]
    topic_ids = [r["id"] for r in db_topics(sci8, nxt)][:3]
    draft = questions.generate_assignment(db, engine, priya, class_id=classes["8A"]["id"], topic_ids=topic_ids, kind="mission",
                                          difficulty="medium", count=5, title="Next chapter: warm-up mission")
    with db.tenant(inst) as tx:
        assignments.set_for_sections(tx, priya, draft, [classes["8B"]["id"], classes["8C"]["id"]])
        # lessons presented on the projector over the last four weeks
        for t, n in ((priya, 22), (arjun, 9), (fatima, 1)):
            for _ in range(n):
                tx.exec("""INSERT INTO audit_events(institution_id, actor_id, action, target, detail, created_at)
                           VALUES (%s,%s,'present.open',%s,%s,%s)""",
                        [inst, t.user_id, "chapter:0", jsonb({"period": rng.randint(1, 6), "demo": True}),
                         now - timedelta(days=rng.randint(0, 27), hours=rng.randint(0, 6))])
        assignments.sync_states(tx)
    logins = {label: [{"roll": k["roll_no"], "name": k["name"], "pin": k["pin"]} for k in c["kids"]] for label, c in classes.items()}
    return {"school": "Green Valley Public School (demo)", "code": CODE, "password": password,
            "staff": [("School admin", "Asha Menon", f"admin@{DOMAIN}"), ("Principal", "Dr Ramesh Kumar", f"principal@{DOMAIN}"),
                      ("Teacher: Science 8A, 8B, 8C (with timetable)", "Mrs Priya Rao", f"priya@{DOMAIN}"),
                      ("Teacher: Maths 8A, 8B (with timetable)", "Mr Arjun Iyer", f"arjun@{DOMAIN}"),
                      ("Teacher: Science 9A, 9B and Maths 8C (no timetable)", "Ms Fatima Sheikh", f"fatima@{DOMAIN}")],
            "students": logins}


def write_logins(info: dict[str, Any], path: Path) -> Path:
    lines = [f"{info['school']}  ·  school code {info['code']}", "", "Staff (email / password):"]
    lines += [f"  {role:52} {name:18} {email} / {info['password']}" for role, name, email in info["staff"]]
    lines += ["", "Students sign in with the school code, their class, roll number and PIN:"]
    for label, kids in info["students"].items():
        lines.append(f"  Class {label}")
        lines += [f"    roll {k['roll']:>2}  PIN {k['pin']}  {k['name']}" for k in kids]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", default="GreenValley2026", help="password for every staff account (10+ characters)")
    args = ap.parse_args()
    settings = load_settings()
    db = init_db(settings.app_database_url, 1, 4)
    try:
        info = seed(db, args.password)
    finally:
        db.close()
    out = write_logins(info, Path(settings.data_dir) / "demo-school-logins.txt")
    print(out.read_text(encoding="utf-8"))
    print(f"(Saved to {out})")


if __name__ == "__main__":
    main()
