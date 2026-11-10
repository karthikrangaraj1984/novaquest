"""1.30.0: a teacher's week across several classes: the timetable, Today, one lesson plan with each section at its own
period, the same work set for several sections at once, plus the quick fixes from the UX review."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from novaquest.core import clock
from novaquest.core.errors import Forbidden, ValidationError
from novaquest.services import assignments, institutions, plans, prep, questions, students, timetable, today
from tests.base import PASSWORD, NQTestCase


class Sections(NQTestCase):
    """Mrs Rao teaches Science in 8B (from make_school), 8A and 8C."""

    def school3(self, n: int = 4):
        school = self.make_school(n_students=n)
        extra = {}
        with self.db.tenant(school.inst) as tx:
            for sec in ("A", "C"):
                cid = institutions.create_class(tx, school.admin, grade="8", section=sec, academic_year="2026-27")
                institutions.assign_teacher(tx, school.admin, teacher_id=school.teacher.user_id, class_id=cid, syllabus_id=school.syllabus_id)
                rows = [{"roll_no": str(i + 1), "name": f"{sec} Student {i + 1}", "parent_email": ""} for i in range(n)]
                for s in students.import_rows(tx, school.teacher, cid, rows, skip_invalid=False).added:
                    students.record_consent(tx, school.teacher, s["enrolment_id"], parent_name="Parent", method="signed_form")
                extra[f"8{sec}"] = cid
        extra["8B"] = school.class_id
        return school, extra

    def chapter8(self, school) -> int:
        with self.db.tenant(school.inst) as tx:
            return tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [school.topics["8.3"]])

    # ---------- timetable ----------
    def test_timetable_add_clash_remove(self):
        """[TT-1] A teacher adds lessons for a class and subject on several days at once; a lesson that overlaps one
        already in the week is refused, naming the clash; only classes and subjects they teach can be added; lessons
        can be removed. The week shows as a grid by day and start time."""
        school, cls = self.school3()
        t, sid = school.teacher, school.syllabus_id
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(timetable.add(tx, t, cls["8A"], sid, [0, 2, 4], time(9, 0), 40), 3)
            with self.assertRaises(ValidationError) as e:
                timetable.add(tx, t, cls["8B"], sid, [2], time(9, 20), 40)
            self.assertIn("8A", e.exception.message)
            timetable.add(tx, t, cls["8B"], sid, [2], time(9, 40), 40)  # right after: fine
            other = institutions.create_class(tx, school.admin, grade="9", section="A", academic_year="2026-27")
            with self.assertRaises((ValidationError, Forbidden)):
                timetable.add(tx, t, other, sid, [1], time(12, 0), 40)
            rows = timetable.slots(tx, t.user_id)
            self.assertEqual([(r["day"], r["class_label"]) for r in rows], [("Mon", "8A"), ("Wed", "8A"), ("Wed", "8B"), ("Fri", "8A")])
            grid = timetable.week_grid(rows)
            self.assertEqual([r["time"] for r in grid["rows"]], [time(9, 0), time(9, 40)])
            timetable.remove(tx, t, rows[0]["id"])
            self.assertEqual(len(timetable.slots(tx, t.user_id)), 3)

    def test_today_in_time_order_with_next_flagged(self):
        """[TT-2] Today lists the day's lessons in time order (done, now, later) with the next one flagged, each with its
        chapter from the term plan, the period that class is on and a Present link for exactly that period; without a
        timetable it lists every class the teacher teaches."""
        school, cls = self.school3()
        t, sid = school.teacher, school.syllabus_id
        ch = self.chapter8(school)
        with self.db.tenant(school.inst) as tx:
            plain = today.classes_today(tx, t)
            self.assertFalse(plain["timetable"])
            self.assertEqual([r["class_label"] for r in plain["rows"]], ["8A", "8B", "8C"])
            for c in cls.values():  # chapter 8 from 5 Oct for every section
                plans.save(tx, t, c, sid, [(ch, date(2026, 10, 5), date(2026, 10, 30))])
            # Thursday 15 Oct, 10:00 IST: 8A at 08:00 (done), 8B at 09:40 (now), 8C at 11:00 (later)
            timetable.add(tx, t, cls["8A"], sid, [0, 3], time(8, 0), 40)
            timetable.add(tx, t, cls["8B"], sid, [3], time(9, 40), 40)
            timetable.add(tx, t, cls["8C"], sid, [1, 3], time(11, 0), 40)
            d = today.classes_today(tx, t)
        self.assertTrue(d["timetable"])
        self.assertEqual([(r["class_label"], r["slot"]["when"], r["slot"]["next"]) for r in d["rows"]],
                         [("8A", "past", False), ("8B", "now", True), ("8C", "later", False)])
        by = {r["class_label"]: r for r in d["rows"]}
        # 8A meets Mon and Thu: since 5 Oct it has had Mon 5, Thu 8, Mon 12 and today's 08:00 lesson -> period 5 next
        self.assertEqual(by["8A"]["pos"]["held"], 4)
        self.assertEqual(by["8B"]["pos"]["held"], 1)  # Thu 8 Oct; today's lesson is still on
        self.assertEqual(by["8C"]["pos"]["period"], 4)  # Tue 6, Thu 8, Tue 13 held; today's 11:00 is later
        self.assertIn(f"/teacher/present/{ch}?for={cls['8B']}:{sid}", by["8B"]["present"])
        self.assertIn("period=2", by["8B"]["present"])
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            st, page = c.get("/teacher")[:2]
            self.assertEqual(st, 200)
            self.assertLess(page.index("09:40"), page.index("11:00"))
            self.assertIn("Today's classes", page)
            self.assertIn(">Today<", page)
            self.assertIn(">Teach<", page)
            st, tt = c.get("/teacher/timetable")[:2]
            self.assertEqual(st, 200)
            self.assertIn("Your week", tt)
        finally:
            live.stop()

    def test_lesson_plan_counts_lessons_and_each_section_has_its_period(self):
        """[TT-3][MC-2] With a timetable a chapter's lesson plan has as many periods as lessons the chapter gets; each
        section is at its own period, shown on its chip and on the period ('Next for 8A'); homework for a class closes
        just before its first lesson at least two days away."""
        school, cls = self.school3()
        t, sid = school.teacher, school.syllabus_id
        ch = self.chapter8(school)
        with self.db.tenant(school.inst) as tx:
            for c in cls.values():
                plans.save(tx, t, c, sid, [(ch, date(2026, 10, 5), date(2026, 10, 16))])
            timetable.add(tx, t, cls["8A"], sid, [0, 2, 4], time(9, 0), 40)  # Mon Wed Fri
            data, _ = prep.pack(tx, ch)
            n = prep.default_periods(tx, cls["8A"], ch, data)
            self.assertEqual(n, 6)  # 5, 7, 9, 12, 14, 16 Oct
            pa = prep.position(tx, cls["8A"], ch, n)
            self.assertEqual((pa["held"], pa["period"]), (5, 6))  # 5, 7, 9, 12 and 14 Oct taught; Fri 16 is next
            due = timetable.due_by(tx, cls["8A"], [sid])
            self.assertEqual(clock.to_ist(due).strftime("%a %H:%M"), "Mon 08:55")  # Sat 17 is 2 days on; next lesson Mon 19
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            page = c.get(f"/teacher/prep?for={cls['8A']}:{sid}&chapter={ch}")[1]
            self.assertIn("Next for 8A", page)
            self.assertIn("lessons your timetable gives", page)
            self.assertIn('class="section-chip"', page)
            self.assertIn("8C · Science", page)
        finally:
            live.stop()

    def test_copy_term_plan_to_other_sections(self):
        """[MC-3] A teacher gives other sections the same chapter dates in one step; each class keeps its own progress,
        and the plan page says where every section is."""
        school, cls = self.school3()
        t, sid = school.teacher, school.syllabus_id
        with self.db.tenant(school.inst) as tx:
            plans.autofill(tx, t, cls["8B"], sid, date(2026, 6, 15), date(2027, 2, 27))
            with self.assertRaises(ValidationError):
                plans.copy_to(tx, t, cls["8A"], sid, [cls["8C"]])  # 8A has no dates to copy
            self.assertEqual(plans.copy_to(tx, t, cls["8B"], sid, [cls["8A"], cls["8C"]]), 2)
            for c in ("8A", "8C"):
                self.assertEqual(plans.view(tx, t, cls[c], sid)["planned"], plans.view(tx, t, cls["8B"], sid)["planned"])
            self.assertIn("now", plans.section_status(tx, t, cls["8A"], sid))
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            page = c.get(f"/teacher/plan?for={cls['8B']}:{sid}")[1]
            self.assertIn("Use these dates for your other sections", page)
        finally:
            live.stop()

    # ---------- the same work for several sections ----------
    def test_one_draft_scheduled_for_several_sections(self):
        """[MC-1] A draft can be meant for other sections of the same subject (ticked when it's made); approved once,
        it is scheduled for every ticked class in one step, each with its own open and close times, and each has its
        own results; the sections link to each other and the teacher's to-do shows them as one row."""
        school, cls = self.school3()
        t = school.teacher
        aid = questions.generate_assignment(self.db, self.engine, t, class_id=cls["8B"], topic_ids=[school.topics["8.3"]],
                                            kind="quiz", difficulty="medium", count=3, title="Cells quiz")
        with self.db.tenant(school.inst) as tx:
            other = institutions.create_class(tx, school.admin, grade="9", section="A", academic_year="2026-27")
            self.assertEqual(assignments.set_for_sections(tx, t, aid, [cls["8A"], cls["8C"], other]), [cls["8A"], cls["8C"]])
            a = assignments.get(tx, aid)
            self.assertEqual([c["label"] for c in assignments.section_choices(tx, t, a) if c["ticked"]], ["8A", "8C"])
            for q in questions.assignment_questions(tx, aid):
                questions.approve(tx, t, q["id"])
            now = clock.now()
            plan = [{"class_id": cls["8B"], "opens_at": now, "expires_at": now + timedelta(days=2)},
                    {"class_id": cls["8A"], "opens_at": now, "expires_at": now + timedelta(days=3)},
                    {"class_id": cls["8C"], "opens_at": now + timedelta(days=1), "expires_at": now + timedelta(days=4)}]
            ids = assignments.schedule_sections(tx, t, aid, plan, targets=[("class", cls["8B"])])
            self.assertEqual(len(ids), 3)
            sibs = assignments.siblings(tx, assignments.get(tx, ids[1]))
            self.assertEqual([s["class_label"] for s in sibs], ["8A", "8B", "8C"])
            self.assertEqual({s["class_label"]: s["live_state"] for s in sibs}, {"8A": "open", "8B": "open", "8C": "scheduled"})
            q_ids = {r["question_id"] for r in tx.all("SELECT question_id FROM assignment_questions WHERE assignment_id = ANY(%s)", [ids])}
            self.assertEqual(len(q_ids), 3, "the same questions, not copies")
            self.assertEqual(len(assignments.target_students(tx, ids[1])), 4)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            home = c.get("/teacher")[1]
            self.assertIn("8A 0/4, 8B 0/4", home.replace("&amp;", "&"))
            review = c.get(f"/teacher/assignments/{ids[0]}/review")[1]
            self.assertIn('class="section-chip"', review)
            lst = c.get(f"/teacher/assignments?class={cls['8C']}")[1]
            self.assertIn("Cells quiz", lst)
            self.assertNotIn(f"/teacher/assignments/{ids[0]}\"", lst)
        finally:
            live.stop()

    def test_schedule_page_offers_sections_with_their_own_dates(self):
        """[MC-1] The draft page lists the other sections (ticked as chosen) each with its own opens and closes (from
        the timetable when there is one), and scheduling from it creates one assignment per ticked class."""
        school, cls = self.school3()
        t, sid = school.teacher, school.syllabus_id
        with self.db.tenant(school.inst) as tx:
            timetable.add(tx, t, cls["8C"], sid, [0], time(9, 0), 40)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            new = c.get(f"/teacher/assignments/new?for={cls['8B']}:{sid}")[1]
            self.assertIn("Also set it for", new)
            st, _, _x, url = c.post("/teacher/assignments/new", {"for": f"{cls['8B']}:{sid}", "topics[]": [str(school.topics["8.3"])],
                                                                 "kind": "quiz", "count": "3", "difficulty": "medium", "sections[]": [str(cls["8C"])]})
            self.assertNotIn("assignments/new", url, _[_.find('role="alert"'):][:300])
            aid = int(url.split("/teacher/assignments/")[1].split("?")[0].split("#")[0])
            page = c.get(f"/teacher/assignments/{aid}")[1]
            self.assertIn("Classes and dates", page)
            self.assertIn(f'name="closes_{cls["8C"]}" type="datetime-local" value="2026-10-19T08:55"', page)
            c.post(f"/teacher/assignments/{aid}/approve-all", {})
            st, body, _, _ = c.post(f"/teacher/assignments/{aid}/schedule", {
                "who": "class", "opens_at": "2026-10-15T10:00", "expires_at": "2026-10-18T18:00", "max_attempts": "1",
                "sections[]": [str(cls["8C"])], f"opens_{cls['8C']}": "2026-10-15T10:00", f"closes_{cls['8C']}": "2026-10-19T08:55"})
            self.assertIn("Scheduled for 8B, 8C", body)
        finally:
            live.stop()
        with self.db.tenant(school.inst) as tx:
            rows = tx.all("SELECT class_id, expires_at FROM assignments WHERE batch_id=%s ORDER BY id", [aid])
        self.assertEqual([r["class_id"] for r in rows], [cls["8B"], cls["8C"]])
        self.assertEqual(clock.to_ist(rows[1]["expires_at"]).strftime("%d %H:%M"), "19 08:55")


class QuickFixes(NQTestCase):
    def test_results_page_fixes(self):
        """[QW-1] Students not handed in are listed 1, 2 … 10 (not 1, 10, 2); release and the class message appear only
        once work is handed in; an open assignment opens on its results (progress) and its questions stay one tab
        away; to-do rows name the class and subject."""
        school = self.make_school(n_students=11)
        aid = self.open_assignment(school)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            st, _, _, url = c.get(f"/teacher/assignments/{aid}")
            self.assertTrue(url.endswith(f"/teacher/assignments/{aid}/review"))
            self.assertEqual(c.get(f"/teacher/assignments/{aid}?view=questions")[0], 200)
            page = c.get(f"/teacher/assignments/{aid}/review")[1]
            self.assertIn("1. Student 1 · 2. Student 2", page)
            self.assertLess(page.index("9. Student 9"), page.index("10. Student 10"))
            self.assertNotIn("Release results to students", page)
            self.assertIn("once work is handed in", page)
            home = c.get("/teacher")[1]
            self.assertIn("8B · Science", home)
        finally:
            live.stop()
        self.answer_all(school, school.student(0), aid)
        from novaquest.services import attempts
        attempts.submit(self.db, self.engine, school.student(0), tx_attempt(self.db, school, aid))
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            self.assertIn("Release results to students", c.get(f"/teacher/assignments/{aid}/review")[1])
        finally:
            live.stop()

    def test_filters_apply_on_change_and_assignment_filters(self):
        """[QW-2] Pickers apply as soon as they change (the Show button is hidden by script); the assignments list
        filters by stage and class with counts; every assignment type shows without a 'More types' fold."""
        school = self.make_school()
        self.open_assignment(school)
        from tests.live import Client, LiveServer
        live = LiveServer(self.settings, self.db)
        try:
            c = Client(live.url)
            c.login("rao@sun.test", PASSWORD)
            prep_page = c.get("/teacher/prep")[1]
            self.assertIn("data-autoapply", prep_page)
            self.assertIn("data-apply", prep_page)
            lst = c.get("/teacher/assignments?show=open")[1]
            self.assertIn("Open (1)", lst)
            self.assertIn("Drafts (0)", lst)
            self.assertIn("Test task", lst)
            self.assertNotIn("Test task", c.get("/teacher/assignments?show=draft")[1])
            new = c.get("/teacher/assignments/new")[1]
            self.assertNotIn("More types", new)
            self.assertIn("Escape room", new)
            js = c.get("/static/app.js")[1]
            self.assertIn("form[data-autoapply]", js)
        finally:
            live.stop()


def tx_attempt(db, school, aid) -> int:
    with db.tenant(school.inst) as tx:
        return tx.val("SELECT id FROM attempts WHERE assignment_id=%s AND student_id=%s", [aid, school.students[0]["student_id"]])
