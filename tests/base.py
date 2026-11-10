"""Shared test harness: one test database per run, a fixed clock, and builders for schools.

Tests are tagged with the requirement IDs they prove, in the docstring: "[ST-1] …"."""
from __future__ import annotations

import os
import threading
import time
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from novaquest.ai.offline import OfflineEngine
from novaquest.core import clock, security
from novaquest.core.config import load_settings
from novaquest.core.db import Db, init_db
from novaquest.core.migrate import bootstrap, migrate
from novaquest.services import institutions, students, syllabus
from novaquest.services.access import Actor

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("NQ_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:54329/postgres")
DB_NAME = os.environ.get("NQ_TEST_DB", "nq_test")
OWNER_URL = ADMIN_URL.replace("postgres@", "nq_owner:nq_owner@").rsplit("/", 1)[0] + "/" + DB_NAME
APP_URL = OWNER_URL.replace("nq_owner:nq_owner", "nq_app:nq_app")
PASSWORD = "TestPass123"

_lock = threading.Lock()
_state: dict = {}


def setup_database() -> Db:
    """Create the test database once per run (fresh schema + the real NCERT syllabus)."""
    with _lock:
        if "db" in _state:
            return _state["db"]
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        bootstrap(ADMIN_URL, OWNER_URL, APP_URL)
        migrate(OWNER_URL, "nq_app")
        os.environ.update(NQ_DATABASE_URL=OWNER_URL, NQ_APP_DATABASE_URL=APP_URL, NQ_SECRET_KEY="test-secret",
                          NQ_SECURE_COOKIES="0", NQ_AI_MODE="offline", NQ_BASE_URL="http://testserver")
        os.environ.pop("ANTHROPIC_API_KEY", None)
        db = init_db(APP_URL, 2, 30)
        pdf = ROOT / "data" / "source" / "ncert_science_class8_2018.pdf"
        syllabus.load_syllabus(db, ROOT / "data" / "syllabus" / "ncert_g8_science.json", pdf if pdf.exists() else None, actor_id=None)
        with psycopg.connect(OWNER_URL) as conn:
            tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()]
            # A snapshot of the shared syllabus (NCERT) to restore after each test: truncating institutions cascades
            # into these tables now that schools can own books.
            conn.execute("DROP SCHEMA IF EXISTS nqsnap CASCADE")
            conn.execute("CREATE SCHEMA nqsnap")
            for t in ("syllabi", "syllabus_nodes", "misconceptions", "lessons"):
                conn.execute(f'CREATE TABLE nqsnap."{t}" AS SELECT * FROM "{t}" WHERE institution_id IS NULL')
            _state["passages"] = conn.execute("""SELECT id, syllabus_id, node_id, source_kind, ref, page, body, embedding::text
                                                 FROM source_passages WHERE institution_id IS NULL ORDER BY id""").fetchall()
            conn.commit()
        _state["tables"] = [t for t in tables if t != "schema_migrations"]
        _state["db"] = db
        return db


def reset_data() -> None:
    """Remove all institution data between tests and restore the shared NCERT syllabus with its original ids."""
    with psycopg.connect(OWNER_URL) as conn:
        conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in _state["tables"]) + " RESTART IDENTITY CASCADE")
        for t in ("syllabi", "syllabus_nodes", "misconceptions", "lessons"):
            conn.execute(f'INSERT INTO "{t}" OVERRIDING SYSTEM VALUE SELECT * FROM nqsnap."{t}"' if t != "lessons"
                         else f'INSERT INTO "{t}" SELECT * FROM nqsnap."{t}"')
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO source_passages(id, syllabus_id, node_id, source_kind, ref, page, body, embedding)
                               OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)""", _state["passages"])
        for t in ("syllabi", "syllabus_nodes", "misconceptions", "source_passages"):
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM \"{t}\"))")
        conn.commit()


@dataclass
class School:
    """A ready-made school: admin, teacher, leader, class 8B and students with consent."""

    inst: int
    admin: Actor
    teacher: Actor
    leader: Actor
    class_id: int
    syllabus_id: int
    topics: dict[str, int]
    students: list[dict] = field(default_factory=list)

    def student(self, i: int = 0) -> Actor:
        s = self.students[i]
        return Actor(s["student_id"], self.inst, "student", ("student",), s["name"])


class NQTestCase(unittest.TestCase):
    """Base class: fresh data, fixed clock (15 Oct 2026, 10:00 IST) and the offline AI engine."""

    db: Db

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = setup_database()
        cls.settings = load_settings()

    def setUp(self) -> None:
        reset_data()
        self.clock = clock.FixedClock(datetime(2026, 10, 15, 4, 30, tzinfo=timezone.utc))
        clock.set_clock(self.clock)
        self.engine = OfflineEngine()

    def tearDown(self) -> None:
        clock.set_clock(clock.Clock())

    # ---------- builders ----------
    def platform_admin(self) -> Actor:
        with self.db.platform() as tx:
            uid = tx.val("INSERT INTO users(name, email, password_hash) VALUES ('NQ Admin', 'nq@admin.test', %s) RETURNING id",
                         [security.hash_secret(PASSWORD)])
            tx.exec("INSERT INTO user_roles(user_id, role) VALUES (%s, 'platform_admin')", [uid])
        return Actor(uid, None, "platform_admin", ("platform_admin",), "NQ Admin")

    def staff(self, inst: int, name: str, email: str, roles: list[str]) -> Actor:
        with self.db.tenant(inst) as tx:
            uid = tx.val("INSERT INTO users(institution_id, name, email, password_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                         [inst, name, email, security.hash_secret(PASSWORD)])
            for r in roles:
                tx.exec("INSERT INTO user_roles(user_id, institution_id, role) VALUES (%s,%s,%s)", [uid, inst, r])
        return Actor(uid, inst, roles[0], tuple(roles), name)

    def make_school(self, code: str = "SUN", n_students: int = 6, consent: bool = True) -> School:
        """A school with staff, class 8B, students and parental consent recorded."""
        with self.db.platform() as tx:
            inst = tx.val("""INSERT INTO institutions(name, kind, board, academic_year, school_code)
                             VALUES (%s,'school','CBSE','2026-27',%s) RETURNING id""", [f"{code} School", code])
        p = code.lower()
        admin = self.staff(inst, "Asha Admin", f"admin@{p}.test", ["inst_admin"])
        teacher = self.staff(inst, "Mrs Rao", f"rao@{p}.test", ["teacher"])
        leader = self.staff(inst, "Dr Principal", f"principal@{p}.test", ["leader"])
        with self.db.tenant(inst) as tx:
            cid = institutions.create_class(tx, admin, grade="8", section="B", academic_year="2026-27")
            sid = syllabus.current_syllabus_id(tx)
            institutions.assign_teacher(tx, admin, teacher_id=teacher.user_id, class_id=cid, syllabus_id=sid)
            topics = {r["code"]: r["id"] for r in tx.all("SELECT id, code FROM syllabus_nodes WHERE kind='topic' AND syllabus_id=%s", [sid])}
            rows = [{"roll_no": str(i + 1), "name": f"Student {i + 1}", "parent_email": f"parent{i + 1}@{p}.test"} for i in range(n_students)]
            added = students.import_rows(tx, teacher, cid, rows, skip_invalid=False).added
            if consent:
                for s in added:
                    students.record_consent(tx, teacher, s["enrolment_id"], parent_name="Parent", method="signed_form")
        return School(inst, admin, teacher, leader, cid, sid, topics, added)

    def open_assignment(self, school: School, kind: str = "mission", topics: tuple[str, ...] = ("8.3", "8.5"), count: int = 5,
                        hours: float = 24, targets: list | None = None, **kw) -> int:
        """Generate, approve and open an assignment for the class."""
        from novaquest.services import assignments, questions
        aid = questions.generate_assignment(self.db, self.engine, school.teacher, class_id=school.class_id,
                                            topic_ids=[school.topics[t] for t in topics], kind=kind, difficulty=kw.pop("difficulty", "medium"),
                                            count=count, title="Test task")
        with self.db.tenant(school.inst) as tx:
            for q in questions.assignment_questions(tx, aid):
                questions.approve(tx, school.teacher, q["id"])
            assignments.schedule(tx, school.teacher, aid, opens_at=clock.now() - timedelta(minutes=1),
                                 expires_at=clock.now() + timedelta(hours=hours), targets=targets or [("class", school.class_id)], **kw)
        return aid

    def answer_all(self, school: School, student: Actor, aid: int, good: bool = True) -> int:
        """Start an attempt and answer every question (correctly if good). Returns the attempt id."""
        from novaquest.services import attempts
        with self.db.tenant(school.inst) as tx:
            att = attempts.start_or_resume(tx, student, aid)
            qs = {q["id"]: q for q in tx.all("""SELECT q.* FROM questions q JOIN assignment_questions aq ON aq.question_id=q.id
                                                WHERE aq.assignment_id=%s""", [aid])}
            for qid, q in qs.items():
                if q["status"] != "approved":
                    continue
                if q["options"]:
                    idx = q["answer"]["index"] if good else (q["answer"]["index"] + 1) % len(q["options"])
                    attempts.save_answer(tx, student, att["id"], qid, {"index": idx})
                elif q["kind"] in ("short", "viva"):
                    text = (q["answer"]["model"] + " For example, " + " ".join(a for r in q["rubric"] for a in r["any"][:1])) if good else "I am not sure."
                    attempts.save_answer(tx, student, att["id"], qid, {"text": text})
        return att["id"]


def timed(fn, *args, **kwargs):
    """Run fn and return (result, seconds)."""
    t = time.perf_counter()
    res = fn(*args, **kwargs)
    return res, time.perf_counter() - t
