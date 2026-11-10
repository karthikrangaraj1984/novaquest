"""Create demo data for the CBSE school demo: a school, a teacher, a principal, classes 8A and 8B with 12 sample
students in 8B (consent recorded), the school's own demo book (an original two-chapter text, uploaded and read like any
school's book), and an open Cell-chapter mission with some submissions.

Usage: python -m tools.seed_demo [--password DemoPass123]"""
from __future__ import annotations

import argparse
import random
from datetime import timedelta

from novaquest.ai.offline import OfflineEngine
from novaquest.core import clock, security
from novaquest.core.config import load_settings
from novaquest.core.db import Db, init_db
import tempfile
from pathlib import Path

from novaquest.services import assignments, attempts, books, institutions, questions, students
from tools import demo_book
from novaquest.services.access import Actor

NAMES = ["Aadhya Ramesh", "Aarav Sharma", "Diya Krishnan", "Kavin Murugan", "Meera Venkat", "Nila Suresh", "Rohan Prasad",
         "Sanjana Iyer", "Tanvi Rao", "Vikram Nair", "Ishaan Gupta", "Zara Khan"]


def _staff(db: Db, inst: int, name: str, email: str, roles: list[str], password: str) -> int:
    with db.tenant(inst) as tx:
        uid = tx.val("INSERT INTO users(institution_id, name, email, password_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                     [inst, name, email, security.hash_secret(password)])
        for r in roles:
            tx.exec("INSERT INTO user_roles(user_id, institution_id, role) VALUES (%s,%s,%s)", [uid, inst, r])
    return uid


def _demo_book(db: Db, admin: Actor, storage: Path) -> int:
    """Upload, read and publish the demo book as the school's own book."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = demo_book.make(Path(tmp) / "novaquest-demo-science-8.pdf")
        with pdf.open("rb") as fh:
            sid = books.create(db, admin, board="CBSE", subject="Science", grade="8", title=demo_book.TITLE,
                               files=[(pdf.name, fh)], storage=storage)
    books.read_book(db, OfflineEngine(), admin.institution_id, sid, storage)
    from tools.demo_school import wait_until_read
    wait_until_read(db, admin.institution_id, sid)
    with db.tenant(admin.institution_id) as tx:
        books.publish(tx, admin, sid)
    return sid


def seed(db: Db, password: str = "DemoPass123", platform_email: str = "admin@novaquest.local",
         storage: Path | None = None) -> dict:
    """Create the demo school. Safe to run once; returns the login details."""
    with db.platform() as tx:
        if tx.val("SELECT 1 FROM institutions WHERE school_code='SVS-CBE'"):
            raise SystemExit("Demo data already exists (school code SVS-CBE).")
        pa = tx.val("SELECT id FROM users WHERE email=%s", [platform_email])
        if not pa:
            pa = tx.val("INSERT INTO users(name, email, password_hash) VALUES ('NovaQuest Admin', %s, %s) RETURNING id",
                        [platform_email, security.hash_secret(password)])
            tx.exec("INSERT INTO user_roles(user_id, role) VALUES (%s, 'platform_admin')", [pa])
        inst = tx.val("""INSERT INTO institutions(name, kind, board, academic_year, school_code)
                         VALUES ('Sri Vidya School (demo)', 'school', 'CBSE', '2026-27', 'SVS-CBE') RETURNING id""")
    admin = _staff(db, inst, "Asha Menon", "admin@svs.demo", ["inst_admin"], password)
    teacher = _staff(db, inst, "Mrs Rao", "rao@svs.demo", ["teacher"], password)
    _staff(db, inst, "Dr Kumar", "principal@svs.demo", ["leader", "teacher"], password)
    admin_actor = Actor(admin, inst, "inst_admin", ("inst_admin",), "Asha Menon")
    t_actor = Actor(teacher, inst, "teacher", ("teacher",), "Mrs Rao")
    with db.tenant(inst) as tx:
        cid = institutions.create_class(tx, admin_actor, grade="8", section="B", academic_year="2026-27")
        cid2 = institutions.create_class(tx, admin_actor, grade="8", section="A", academic_year="2026-27")
    sid = _demo_book(db, admin_actor, storage or books.storage_dir(load_settings().data_dir))
    with db.tenant(inst) as tx:
        for c in (cid, cid2):
            institutions.set_class_subjects(tx, admin_actor, c, [sid])
        institutions.assign_teacher(tx, admin_actor, teacher_id=teacher, class_id=cid, syllabus_id=sid)
        institutions.assign_teacher(tx, admin_actor, teacher_id=teacher, class_id=cid2, syllabus_id=sid)
        rows = [{"roll_no": str(i + 1), "name": n, "parent_email": f"parent{i + 1}@example.com"} for i, n in enumerate(NAMES)]
        added = students.import_rows(tx, t_actor, cid, rows, skip_invalid=False).added
        for s in students.list_students(tx, cid):
            students.record_consent(tx, t_actor, s["enrolment_id"], parent_name="Parent (sample data)", method="signed_form")
        topics = {r["code"]: r["id"] for r in tx.all("SELECT id, code FROM syllabus_nodes WHERE kind='topic' AND syllabus_id=%s", [sid])}
    engine = OfflineEngine()
    aid = questions.generate_assignment(db, engine, t_actor, class_id=cid, topic_ids=[topics["1.2"], topics["1.3"], topics["1.4"]],
                                        kind="mission", difficulty="medium", count=6, title="Cell Lab mission")
    with db.tenant(inst) as tx:
        for q in questions.assignment_questions(tx, aid):
            questions.approve(tx, t_actor, q["id"])
        assignments.schedule(tx, t_actor, aid, opens_at=clock.now() - timedelta(hours=2), expires_at=clock.now() + timedelta(days=1, hours=4),
                             targets=[("class", cid)])
    rng = random.Random(8)
    for a in added[:7]:
        s_actor = Actor(a["student_id"], inst, "student", ("student",), a["name"])
        with db.tenant(inst) as tx:
            att = attempts.start_or_resume(tx, s_actor, aid)
            for q in attempts.view(tx, s_actor, att["id"])["questions"]:
                if q["options"]:
                    attempts.save_answer(tx, s_actor, att["id"], q["id"], {"index": rng.randrange(len(q["options"]))})
                else:
                    attempts.save_answer(tx, s_actor, att["id"], q["id"], {"text": rng.choice([
                        "Plant cells have a cell wall and big vacuole and chloroplasts, animal cells do not.",
                        "The cell wall protects the plant cell.", "nucleus controls"])})
        attempts.submit(db, engine, s_actor, att["id"])
    return {"school_code": "SVS-CBE", "class": "8B", "students": added, "password": password,
            "staff": {"platform admin": platform_email, "school admin": "admin@svs.demo", "teacher": "rao@svs.demo",
                      "principal (also teaches)": "principal@svs.demo"}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--password", default="DemoPass123")
    args = ap.parse_args()
    settings = load_settings()
    db = init_db(settings.app_database_url, 1, 4)
    try:
        info = seed(db, args.password)
    finally:
        db.close()
    print(f"Demo school created. School code {info['school_code']}, class {info['class']}.")
    for role, email in info["staff"].items():
        print(f"  {role:26} {email} / {info['password']}")
    print("  Student logins (school code, class, roll, PIN):")
    for s in info["students"]:
        print(f"    {info['school_code']}  {info['class']}  roll {s['roll_no']:>2}  PIN {s['pin']}  {s['name']}")


if __name__ == "__main__":
    main()
