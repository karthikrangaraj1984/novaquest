"""Escape rooms: learn AI through AI (LN-12, DB-8), and one measure of progress for students (SX-8)."""
from __future__ import annotations

import re

from novaquest.core.errors import Conflict, Forbidden, NotFound, ValidationError
from novaquest.services import attempts, dashboards, escape, mastery, review
from tests.base import PASSWORD, NQTestCase


def right_answer(lock: dict) -> dict:
    """The answer that opens a lock."""
    if lock["kind"] == "sort":
        return {"features": list(min(escape.solving_sets(lock), key=len))}
    if lock["kind"] == "clean":
        return {"flag": [i for i, r in enumerate(lock["rows"]) if r.get("wrong")]}
    if lock["kind"] == "evidence":
        return {"verdict": lock["verdict"], "evidence": lock["right"]}
    return {"choice": lock["answer"]}


def wrong_answer(lock: dict) -> dict:
    if lock["kind"] == "sort":
        keys = [f["key"] for f in lock["features"]]
        bad = next(k for k in keys if (k,) not in escape.solving_sets(lock))
        return {"features": [bad]}
    if lock["kind"] == "clean":
        return {"flag": [next(i for i, r in enumerate(lock["rows"]) if not r.get("wrong"))]}
    if lock["kind"] == "evidence":
        return {"verdict": 1 - lock["verdict"], "evidence": lock["right"]}
    return {"choice": (lock["answer"] + 1) % len(lock["options"])}


class RoomContent(NQTestCase):
    def test_every_chapter_has_a_valid_room(self):
        """[LN-12] All 18 chapters of the NCERT book have a room with exactly four locks on that chapter's topics; across
        the rooms all four kinds of lock and all six AI skills are used; every sorting lock can be solved, and in every
        one some clue fools NOVA on its own (the choice of clues matters); every room has its own 4-digit door code."""
        rooms = escape.rooms()
        self.assertEqual(sorted(rooms), list(range(1, 19)))
        kinds = {lk["kind"] for r in rooms.values() for lk in r["locks"]}
        skills = {lk["skill"] for r in rooms.values() for lk in r["locks"]}
        self.assertEqual(kinds, set(escape.KINDS))
        self.assertEqual(skills, set(escape.SKILLS))
        for no, room in rooms.items():
            self.assertEqual(len(room["locks"]), 4)
            for lk in room["locks"]:
                self.assertTrue(lk["topic"].startswith(f"{no}."), (no, lk["topic"]))
                if lk["kind"] == "sort":
                    sets = escape.solving_sets(lk)
                    self.assertTrue(sets)
                    self.assertTrue(any((f["key"],) not in sets for f in lk["features"]), (no, lk["title"]))
        codes = [escape.code_for(n) for n in rooms]
        self.assertTrue(all(re.fullmatch(r"\d{4}", c) for c in codes))
        self.assertGreater(len(set(codes)), 15)

    def test_validator_rejects_broken_rooms(self):
        """[LN-12] A broken room never reaches students: an unsolvable sorter, a sorter where any clue works, a cleaning
        lock with nothing wrong, and an answer out of range are all refused when loading."""
        room = escape.rooms()[8]
        topics = {f"8.{i}" for i in range(1, 7)}
        sort_lock = dict(room["locks"][0])
        unsolvable = dict(sort_lock, test=[dict(c, label=1 - c["label"]) for c in sort_lock["test"]])
        anything = dict(sort_lock, features=[f for f in sort_lock["features"] if f["key"] == "wall"],
                        train=[dict(c, f={"wall": c["f"]["wall"]}) for c in sort_lock["train"]],
                        test=[dict(c, f={"wall": c["f"]["wall"]}) for c in sort_lock["test"]])
        clean = next(lk for lk in room["locks"] if lk["kind"] == "clean")
        nothing_wrong = dict(clean, rows=[{"item": r["item"], "label": r["label"]} for r in clean["rows"]])
        pat = next(lk for lk in room["locks"] if lk["kind"] == "pattern")
        out_of_range = dict(pat, answer=9)
        for bad in (unsolvable, anything, nothing_wrong, out_of_range):
            with self.assertRaises(escape.ContentError):
                escape.validate_lock(bad, "test", topics)


class PlayingARoom(NQTestCase):
    def test_locks_open_in_order_score_by_tries_and_hints_and_the_code_opens_the_door(self):
        """[LN-12][DB-8] A student plays the Cell room. Locks open in order (trying a later one is refused). A wrong
        try is explained, not revealed (a sorter shows how NOVA sorted each new case); a hint costs a little. Each solve
        gives a digit and what NOVA learned, and counts as a learning event on the lock's topic (Learning, never
        Practised on its own). With all four, a wrong code is refused and the right one escapes."""
        school = self.make_school()
        st = school.student(0)
        room = escape.get_room(8)
        with self.db.tenant(school.inst) as tx:
            v = escape.view(tx, st, 8)
            self.assertEqual(v["current"]["no"], 1)
            with self.assertRaises(Conflict):
                escape.attempt(tx, st, 8, 2, right_answer(room["locks"][1]))
            with self.assertRaises(ValidationError):
                escape.attempt(tx, st, 8, 1, {"features": []})
            r = escape.attempt(tx, st, 8, 1, wrong_answer(room["locks"][0]))
            self.assertFalse(r["solved"])
            self.assertTrue(r["detail"]["results"])
            self.assertTrue(any(not x["ok"] for x in r["detail"]["results"]))
            self.assertIn(room["locks"][0]["hint"], escape.hint(tx, st, 8, 1))
            r = escape.attempt(tx, st, 8, 1, right_answer(room["locks"][0]))
            self.assertTrue(r["solved"])
            self.assertEqual(r["score"], 0.6, "one wrong try (-0.25) and one hint (-0.15)")
            self.assertEqual(r["digit"], escape.code_for(8)[0])
            self.assertEqual(r["learn"], room["locks"][0]["learn"])
            topic = school.topics[room["locks"][0]["topic"]]
            self.assertEqual(mastery.for_student(tx, st.user_id)[topic]["level"], 1)
            clean_no = next(i for i, lk in enumerate(room["locks"], 1) if lk["kind"] == "clean")
            for n in range(2, 5):
                lk = room["locks"][n - 1]
                if n == clean_no:
                    wrong_rows = [i for i, x in enumerate(lk["rows"]) if x.get("wrong")]
                    partial = escape.attempt(tx, st, 8, n, {"flag": wrong_rows[:1] + [next(i for i, x in enumerate(lk["rows"]) if not x.get("wrong"))]})
                    self.assertIn("actually right", partial["message"])
                r = escape.attempt(tx, st, 8, n, right_answer(lk))
                self.assertTrue(r["solved"], lk["title"])
            with self.assertRaises(Conflict):
                escape.hint(tx, st, 8, 1)
            self.assertFalse(escape.open_door(tx, st, 8, "0000" if escape.code_for(8) != "0000" else "1111"))
            self.assertTrue(escape.open_door(tx, st, 8, escape.code_for(8)))
            self.assertTrue(escape.view(tx, st, 8)["escaped"])
            skills = {s["key"]: s for s in escape.student_skills(tx, st.user_id)}
            self.assertEqual(skills["features"]["level"], "learning", "one lock, and not a clean first try")
            self.assertEqual(sum(s["locks"] for s in skills.values()), 4)

    def test_door_needs_every_lock_and_rooms_follow_the_class_books(self):
        """[LN-12] The door can't be opened before all four locks; a class that doesn't study the NCERT book has no
        rooms; a student can't open a teacher's answer guide."""
        school = self.make_school()
        st = school.student(0)
        with self.db.tenant(school.inst) as tx:
            with self.assertRaises(Conflict):
                escape.open_door(tx, st, 8, escape.code_for(8))
            self.assertEqual(len(escape.student_rooms(tx, st)), 18)
            with self.assertRaises(Forbidden):
                escape.guide(tx, st, 8, school.syllabus_id)
        with self.db.tenant(school.inst) as tx:
            tx.exec("DELETE FROM class_subjects WHERE class_id=%s", [school.class_id])  # the class drops the NCERT book
            self.assertEqual(escape.student_rooms(tx, st), [])
            with self.assertRaises(NotFound):
                escape.view(tx, st, 8)

    def test_teachers_and_principal_see_ai_skills_from_the_rooms(self):
        """[DB-8][DB-5] The class view shows every student x room (escaped, locks open, not started) and each AI skill's
        average and how many students are strong; the principal's AI skills come from the rooms; another school sees
        none of it."""
        school = self.make_school(n_students=3)
        a, b = school.student(0), school.student(1)
        with self.db.tenant(school.inst) as tx:
            for no in (8, 2):
                for n, lk in enumerate(escape.get_room(no)["locks"], 1):
                    escape.attempt(tx, a, no, n, right_answer(lk))
                escape.open_door(tx, a, no, escape.code_for(no))
            escape.attempt(tx, b, 8, 1, right_answer(escape.get_room(8)["locks"][0]))
            cv = escape.class_view(tx, school.teacher, school.class_id)
            sid = school.syllabus_id
            self.assertEqual(cv["cells"][(a.user_id, sid, 8)], "escaped")
            self.assertEqual(cv["cells"][(b.user_id, sid, 8)], 1)
            self.assertNotIn((school.student(2).user_id, sid, 8), cv["cells"])
            self.assertEqual(cv["escapes"], 2)
            feats = next(s for s in cv["skills"] if s["key"] == "features")
            self.assertEqual((feats["avg"], feats["students"], feats["strong"]), (100, 2, 1))
            lead = dashboards.leadership(tx, school.leader)
            self.assertIn("Sorting by clues", {s["concept"] for s in lead["ai_skills"]})
            g = escape.guide(tx, school.teacher, 8, school.syllabus_id)
            self.assertEqual(g["code"], escape.code_for(8))
            self.assertIn("Looks green", g["locks"][0]["trap"])
        other = self.make_school("MOON")
        with self.db.tenant(other.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM escape_runs"), 0)
            with self.assertRaises(Exception):
                escape.class_view(tx, other.teacher, school.class_id)


class OverHttp(NQTestCase):
    def test_student_plays_with_forms_and_teacher_sees_the_class(self):
        """[LN-12][DB-8][UX-1] Over HTTP: Learn links to the escape rooms and to each chapter's room. A student solves
        the first lock with the form (after a wrong try that shows NOVA's sorting), opens the rest, types the code and
        sees the celebration and what each lock taught; Me shows AI skills. The teacher's class has an Escape rooms tab
        with the grid and a guide per room."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        room = escape.get_room(8)
        live = LiveServer(self.settings, self.db)
        try:
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = s.get("/student/learn")[1]
            self.assertIn('href="/student/escape"', html)
            self.assertIn(f'href="/student/escape/8?book={school.syllabus_id}"', html)
            html = s.get("/student/escape")[1]
            self.assertIn("0 of 18 rooms escaped", html)
            html = s.get("/student/escape/8")[1]
            self.assertIn(room["story"][:40], html)
            self.assertIn("What NOVA has seen so far", html)
            html = s.post("/student/escape/8", {"action": "try", "features[]": ["green"]})[1]
            self.assertIn("Onion peel cell", html)
            self.assertIn("✗", html)
            html = s.post("/student/escape/8", {"action": "try", "features[]": ["wall"]})[1]
            self.assertIn("Lock 1 is open!", html)
            self.assertIn("What NOVA learned", html)
            for n, lk in enumerate(room["locks"][1:], 2):
                ans = right_answer(lk)
                form = {"action": "try"}
                if "flag" in ans:
                    form["flag[]"] = [str(i) for i in ans["flag"]]
                else:
                    form.update({k: str(v) for k, v in ans.items()})
                html = s.post("/student/escape/8", form)[1]
                self.assertIn(f"Lock {n} is open!", html)
            html = s.get("/student/escape/8")[1]
            self.assertIn("Type the four digits", html)
            html = s.post("/student/escape/8", {"action": "door", "code": "9999" if escape.code_for(8) != "9999" else "1111"})[1]
            self.assertIn("That code didn", html)
            status, html, _, url = s.post("/student/escape/8", {"action": "door", "code": escape.code_for(8)})
            self.assertIn("You escaped! 🎉", html)
            self.assertIn("What each lock taught", html)
            html = s.get("/student/progress")[1]
            self.assertIn("AI skills", html)
            self.assertIn("1 of 18 escape rooms", html)
            t = Client(live.url)
            t.login("rao@sun.test", PASSWORD)
            html = t.get(f"/teacher/classes/{school.class_id}/students")[1]
            self.assertIn(f'href="/teacher/classes/{school.class_id}/escape"', html)
            html = t.get(f"/teacher/classes/{school.class_id}/escape")[1]
            self.assertIn("Who has escaped which room", html)
            self.assertIn('title="Escaped">✓', html)
            html = t.get(f"/teacher/escape/8?book={school.syllabus_id}")[1]
            self.assertIn("Clues that fool NOVA on their own", html)
            self.assertIn(escape.code_for(8), html)
        finally:
            live.stop()


class OneProgressMeasure(NQTestCase):
    def test_students_see_topic_levels_not_percentages(self):
        """[SX-8] Students see topic levels as their one measure: a scorecard says how many were right and the level of
        each topic (no Science or AI skills percentages, no 'big ideas'); Me lists results as 'X of Y right'; the
        parent page and PDF use the same; a topic page names the step that reaches the next level."""
        from tests.live import Client, LiveServer

        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=3)
        st = school.student(0)
        attempts.submit(self.db, self.engine, st, self.answer_all(school, st, aid))
        with self.db.tenant(school.inst) as tx:
            review.release(tx, school.teacher, aid)
            from novaquest.services import reports
            link = reports.create_share_link(tx, st, "http://x")
            pdf = reports.report_pdf(tx, st)
        live = LiveServer(self.settings, self.db)
        try:
            s = Client(live.url)
            s.login_student("SUN", "8B", "1", school.students[0]["pin"])
            html = s.get(f"/student/results/{aid}")[1]
            self.assertIn("3 of 3 right", html)
            self.assertIn("Your topics now", html)
            self.assertRegex(html, r"Parts of the Cell</span><span class=\"pill l\d\">")
            for gone in ('<span class="k">Science</span>', '<span class="k">AI skills</span>', "big idea"):
                self.assertNotIn(gone, html)
            html = s.get("/student/progress")[1]
            self.assertIn("3 of 3 right", html)
            self.assertNotIn('<span class="k">Science</span>', html)
            html = s.get(f"/student/learn/{school.topics['8.3']}")[1]
            self.assertIn("Next: Learn in pictures", html)
            html = Client(live.url).get(link.replace("http://x", ""))[1]
            self.assertIn("3 of 3 right", html)
            self.assertNotIn("Science ", html.split("Recent results")[1][:400])
        finally:
            live.stop()
        import io

        import pypdf
        text = pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        self.assertIn("3 of 3 right", text)
        self.assertNotIn("AI skills", text)
