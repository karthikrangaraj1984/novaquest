"""Real-browser tests with Playwright/Chromium: phone flows, offline autosave, accessibility, PWA
(NF-A11Y, NF-OFF, NF-PWA, SX-3, TA-10)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

from tests.base import PASSWORD, ROOT, NQTestCase
from tests.live import LiveServer

SHOTS = ROOT / "var" / "test-screens"
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
LAPTOP = {"viewport": {"width": 1366, "height": 900}}


def _lum(hex_: str) -> float:
    rgb = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


A11Y_JS = """() => {
  const problems = [];
  document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(el => {
    const id = el.id, lab = id && document.querySelector('label[for="' + id + '"]');
    if (!lab && !el.closest('label') && !el.getAttribute('aria-label')) problems.push('unlabelled ' + el.outerHTML.slice(0, 80));
  });
  document.querySelectorAll('button, a.btn, .icon-btn').forEach(el => {
    const name = (el.textContent || '').trim() || el.getAttribute('aria-label');
    if (!name) problems.push('nameless control ' + el.outerHTML.slice(0, 80));
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.height < 36 || r.width < 36) && !el.classList.contains('link-btn')) problems.push('small target ' + el.outerHTML.slice(0, 60) + ' ' + r.width + 'x' + r.height);
  });
  if (document.querySelectorAll('h1').length !== 1) problems.push('h1 count ' + document.querySelectorAll('h1').length);
  if (!document.documentElement.lang) problems.push('no lang');
  if (document.documentElement.scrollWidth > window.innerWidth + 1) problems.push('horizontal scroll ' + document.documentElement.scrollWidth);
  return problems;
}"""


class Browser(NQTestCase):
    def setUp(self):
        super().setUp()
        self.live = LiveServer(self.settings, self.db)
        SHOTS.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.live.stop()
        super().tearDown()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_design_tokens_meet_contrast(self):
        """[NF-A11Y] Text colours meet WCAG AA contrast (4.5:1) on their backgrounds."""
        css = (ROOT / "novaquest/web/static/app.css").read_text()
        tok = dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", css))
        pairs = [("ink", "bg"), ("muted", "surface"), ("muted", "bg"), ("primary", "surface"), ("primary-dark", "primary-tint"),
                 ("ok-ink", "ok-wash"), ("warn", "warn-wash"), ("ai", "ai-wash"), ("amber-ink", "amber"), ("soft", "bg")]
        bad = [(a, b, round(contrast(tok[a], tok[b]), 2)) for a, b in pairs if contrast(tok[a], tok[b]) < 4.5]
        self.assertEqual(bad, [])
        self.assertGreaterEqual(contrast("#FFFFFF", tok["primary"]), 4.5)
        self.assertGreaterEqual(contrast("#FFFFFF", tok["accent"]), 3.0)  # large bold button text (AA large)

    def test_student_phone_flow_offline_autosave_and_accessibility(self):
        """[SX-3][NF-OFF][NF-A11Y] On a phone: sign in, answer with the Wi-Fi off (answers kept on the device),
        reconnect and they save; every screen passes the accessibility checks."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="mission", count=5)
        pin = school.students[0]["pin"]
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                ctx = await browser.new_context(**PHONE)
                page = await ctx.new_page()
                js_errors = []
                page.on("pageerror", lambda e: js_errors.append(str(e)))
                await page.goto(base + "/login/student")
                problems = {"login": await page.evaluate(A11Y_JS)}
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                problems["home"] = await page.evaluate(A11Y_JS)
                await page.screenshot(path=str(SHOTS / "phone-home.png"))
                await page.click(".due-row")  # 1.31.0: Home's "Due soon"
                await page.wait_for_url("**/attempts/**")
                problems["attempt"] = await page.evaluate(A11Y_JS)
                await ctx.set_offline(True)
                await page.locator(".choice").first.click()
                await page.wait_for_timeout(1500)
                status_offline = await page.text_content("#save-status")
                queued = await page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('nq-queue')).length")
                await ctx.set_offline(False)
                await page.evaluate("window.dispatchEvent(new Event('online'))")
                await page.wait_for_timeout(1500)
                status_online = await page.text_content("#save-status")
                await page.reload()
                checked = await page.locator("input[type=radio]:checked").count()
                await page.screenshot(path=str(SHOTS / "phone-attempt.png"), full_page=True)
                for path in ["/student/learn", f"/student/learn/{school.topics['8.5']}", f"/student/teach/{school.topics['8.5']}",
                             "/student/progress"]:
                    await page.goto(base + path)
                    problems[path] = await page.evaluate(A11Y_JS)
                await page.goto(base + f"/student/teach/{school.topics['8.5']}")
                await page.click(".chip >> nth=0")
                starter = await page.input_value("#reply")
                await page.fill("#reply", "Only plant cells have a cell wall. Animal cells like cheek cells only have a membrane.")
                await page.click("button.send")
                await page.wait_for_selector(".status-chip.ok", timeout=5000)
                await page.screenshot(path=str(SHOTS / "phone-teach.png"), full_page=True)
                await browser.close()
                return problems, js_errors, status_offline, queued, status_online, checked, starter
        problems, js_errors, offline, queued, online, checked, starter = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        self.assertEqual(js_errors, [])
        self.assertIn("Offline", offline)
        self.assertEqual(queued, 1)
        self.assertEqual(online, "All answers saved")
        self.assertEqual(checked, 1, "the answer typed offline was saved on the server")
        self.assertTrue(starter.startswith("Actually"))
        with self.db.tenant(school.inst) as tx:
            self.assertEqual(tx.val("SELECT count(*) FROM responses"), 1)

    def test_student_signs_out_from_header_on_a_shared_phone(self):
        """[SX-7][NF-A11Y] On a phone the account button in the header opens a menu with Sign out. If answers typed
        offline are still waiting on the phone, the student is warned first; signing out then clears them."""
        school = self.make_school()
        pin = school.students[0]["pin"]
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                ctx = await browser.new_context(**PHONE)
                page = await ctx.new_page()
                js_errors = []
                page.on("pageerror", lambda e: js_errors.append(str(e)))
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(base + "/student/learn")
                menu = page.locator("[data-account] .account-menu")
                self.assertFalse(await menu.is_visible())
                await page.click("[data-account] summary")
                self.assertTrue(await menu.is_visible())
                await page.screenshot(path=str(SHOTS / "phone-account-menu.png"))
                box = await menu.bounding_box()
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"] + box["width"], 390)
                self.assertEqual(await page.evaluate(A11Y_JS), [])
                await page.keyboard.press("Escape")
                self.assertFalse(await menu.is_visible())
                await page.click("[data-account] summary")
                await page.click("h1")  # tapping elsewhere (here, the page heading) closes it
                self.assertFalse(await menu.is_visible())
                # An answer still queued on the phone (typed while offline).
                await page.evaluate("""() => localStorage.setItem('nq-queue-/student/attempts/1/save', JSON.stringify({"7": "A"}))""")
                await page.click("[data-account] summary")
                await page.click("[data-account] button[type=submit]")
                warning = page.locator("[data-account] [data-signout-warning]")
                await warning.wait_for(state="visible")
                self.assertIn("1 answer hasn't reached NovaQuest yet", await warning.inner_text())
                self.assertIn("/student/learn", page.url)
                await page.click("[data-account] button[type=submit]")
                await page.wait_for_url("**/login/student")
                self.assertEqual(await page.evaluate(
                    "() => Object.keys(localStorage).filter(k => k.startsWith('nq-queue-')).length"), 0)
                await page.goto(base + "/student")
                self.assertRegex(page.url, r"/login(/student)?$")
                await browser.close()
                return js_errors

        self.assertEqual(self.run_async(flow()), [])

    def test_picture_lessons_on_a_phone(self):
        """[LN-10][NF-A11Y] On a phone, a picture lesson shows one card at a time with progress, Back/Next, arrow keys
        and swipe-free buttons; labels can be hidden to self-test; the picture quiz and Finish work; and every one of
        the 125 picture lessons lays out without sideways scrolling or script errors."""
        from novaquest.services import pictures

        school = self.make_school()
        pin = school.students[0]["pin"]
        base = self.live.url
        with self.db.platform() as tx:
            ids = {r["code"]: r["id"] for r in tx.all("SELECT id, code FROM syllabus_nodes WHERE kind='topic'")}
        lesson = pictures.all_lessons()["8.5"]
        n = len(lesson["cards"]) + 2

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                ctx = await browser.new_context(**PHONE)
                page = await ctx.new_page()
                js_errors = []
                page.on("pageerror", lambda e: js_errors.append(str(e)))
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(f"{base}/student/learn/{ids['8.5']}/pictures")
                visible = page.locator("[data-card]:visible")
                self.assertEqual(await visible.count(), 1)
                self.assertEqual(await page.inner_text("[data-progress] .count"), f"Card 1 of {n}")
                self.assertTrue(await page.is_disabled("[data-prev]"))
                problems = {"deck": await page.evaluate(A11Y_JS)}
                # tap a legend item: its marker lights up
                await page.click("[data-card]:visible button.lg >> nth=0")
                self.assertEqual(await page.locator("[data-card]:visible g.mk.on").count(), 1)
                # self-test: hide the names, reveal one by tapping
                await page.click("[data-card]:visible [data-hide-names]")
                fig = page.locator("[data-card]:visible figure[data-scene]")
                self.assertIn("hidden-names", await fig.get_attribute("class"))
                self.assertEqual(await page.get_attribute("[data-card]:visible [data-hide-names]", "aria-pressed"), "true")
                await page.click("[data-card]:visible button.lg >> nth=1")
                self.assertEqual(await page.locator("[data-card]:visible button.lg.shown").count(), 1)
                await page.screenshot(path=str(SHOTS / "phone-pictures-card.png"))
                await page.click("[data-next]")
                self.assertEqual(await page.inner_text("[data-progress] .count"), f"Card 2 of {n}")
                await page.keyboard.press("ArrowRight")
                await page.keyboard.press("ArrowLeft")
                self.assertEqual(await page.inner_text("[data-progress] .count"), f"Card 2 of {n}")
                for _ in range(n - 2):
                    await page.click("[data-next]")
                self.assertEqual(await page.inner_text("[data-progress] .count"), f"Card {n} of {n}")
                self.assertFalse(await page.is_visible("[data-next]"), "no Next on the last card")
                problems["quiz"] = await page.evaluate(A11Y_JS)
                form = page.locator("form[data-index='0']")
                await form.locator("label.choice >> nth=1").click()
                await form.locator("button[type=submit]").click()
                await form.locator(".result .note").wait_for()
                self.assertIn("Not quite", await form.locator(".result").inner_text())
                await form.locator("label.choice >> nth=0").click()
                await form.locator("button[type=submit]").click()
                await form.locator(".result .note.ok").wait_for()
                self.assertIn("Yes!", await form.locator(".result").inner_text())
                await page.screenshot(path=str(SHOTS / "phone-pictures-quiz.png"), full_page=True)
                # reload: the deck remembers the card you were on
                await page.reload()
                self.assertEqual(await page.inner_text("[data-progress] .count"), f"Card {n} of {n}")
                # Finish goes straight on to the next step of the topic's path (UX-1)
                self.assertIn("Practise", await page.inner_text("[data-finish]"))
                await page.click("[data-finish]")
                await page.wait_for_url("**/practice")
                await page.goto(f"{base}/student/learn/{ids['8.5']}/pictures")
                await page.evaluate("() => sessionStorage.clear()")
                await page.goto(f"{base}/student/learn/{ids['8.5']}")
                self.assertIn("Next: Practise", await page.inner_text("main"))  # the step that leads to the next level
                # every lesson lays out on a phone with all cards shown
                overflow = {}
                for code in sorted(pictures.all_lessons(), key=lambda c: [int(x) for x in c.split(".")]):
                    await page.goto(f"{base}/student/learn/{ids[code]}/pictures")
                    width = await page.evaluate("""() => { document.querySelectorAll('[data-card]').forEach(c => c.hidden = false);
                        return document.documentElement.scrollWidth; }""")
                    if width > 391:
                        overflow[code] = width
                await browser.close()
                return problems, overflow, js_errors

        problems, overflow, js_errors = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        self.assertEqual(overflow, {}, "lessons wider than the phone screen")
        self.assertEqual(js_errors, [])

    def test_teacher_laptop_flow_keyboard_and_accessibility(self):
        """[NF-A11Y] Teacher screens pass the checks, have a skip link and work by keyboard; [AS-5] approve and schedule in the browser."""
        school = self.make_school()
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await (await browser.new_context(**LAPTOP)).new_page()
                await page.goto(base + "/login")
                await page.keyboard.press("Tab")
                skip = await page.evaluate("document.activeElement.textContent")
                await page.focus("#email")
                await page.keyboard.type("rao@sun.test")
                await page.keyboard.press("Tab")
                await page.keyboard.type(PASSWORD)
                await page.keyboard.press("Enter")
                await page.wait_for_url("**/teacher")
                problems = {"home": await page.evaluate(A11Y_JS)}
                await page.goto(base + "/teacher/assignments/new")
                problems["new"] = await page.evaluate(A11Y_JS)
                await page.check(f"input[value='{school.topics['8.5']}']")
                await page.check("input[name=kind][value=quiz]")
                await page.click("summary:has-text('Options')")
                await page.fill("#count", "3")
                await page.click("button:has-text('Write the questions')")
                await page.wait_for_url(re.compile(r".*/teacher/assignments/\d+.*"))
                problems["draft"] = await page.evaluate(A11Y_JS)
                # one main action at a time: approve everything, then schedule (UX-1)
                await page.click("button:has-text('Approve all')")
                await page.wait_for_url(re.compile(r".*msg=Approved.*#sch-h$"))
                self.assertEqual(await page.locator("form[action$='/approve'] button").count(), 0)
                await page.screenshot(path=str(SHOTS / "laptop-draft.png"), full_page=True)
                await page.click("button:has-text('Schedule')")
                await page.wait_for_url("**/teacher?msg=*")
                flash = await page.text_content(".flash")
                for path in ["/teacher/syllabus", f"/teacher/classes/{school.class_id}/students", f"/teacher/classes/{school.class_id}/mastery",
                             f"/teacher/classes/{school.class_id}/growth", f"/teacher/classes/{school.class_id}/briefing"]:
                    await page.goto(base + path)
                    problems[path] = await page.evaluate(A11Y_JS)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.goto(base + "/teacher")
                problems["teacher-on-phone"] = await page.evaluate(A11Y_JS)
                await browser.close()
                return skip, problems, flash
        skip, problems, flash = self.run_async(flow())
        self.assertEqual(skip, "Skip to content")
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        self.assertIn("Scheduled", flash)

    def test_installable_pwa(self):
        """[NF-PWA] The web app is installable on phones: manifest, icon and a service worker that caches the app shell."""
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await (await browser.new_context(**PHONE)).new_page()
                await page.goto(base + "/login/student")
                manifest = await page.evaluate("fetch('/manifest.webmanifest').then(r => r.json())")
                sw = await page.evaluate("fetch('/sw.js').then(r => [r.status, r.headers.get('content-type')])")
                offline = await page.evaluate("fetch('/offline').then(r => r.status)")
                await browser.close()
                return manifest, sw, offline
        manifest, sw, offline = self.run_async(flow())
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(manifest["icons"])
        self.assertEqual(sw[0], 200)
        self.assertIn("javascript", sw[1])
        self.assertEqual(offline, 200)

    def test_data_protection_pages_accessible(self):
        """[PR-5][NF-A11Y] The school admin's Data protection page and the principal's AI conversation log pass the
        accessibility checks on a laptop and a phone, and the erase form opens from the register."""
        from novaquest.services import learn

        school = self.make_school(n_students=3)
        learn.ask(self.db, self.engine, school.student(0), "What does the cell membrane do?", school.topics["8.5"])
        base = self.live.url

        async def flow():
            problems = {}
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                for who, path, shot in (("admin@sun.test", "/admin/privacy", "data-protection"),
                                        ("principal@sun.test", "/leader/ai-log", "ai-log")):
                    for size, opts in (("laptop", LAPTOP), ("phone", PHONE)):
                        page = await (await browser.new_context(**opts)).new_page()
                        await page.goto(base + "/login")
                        await page.fill("#email", who)
                        await page.fill("#password", PASSWORD)
                        await page.click("button[type=submit]")
                        await page.wait_for_load_state()
                        await page.goto(base + path)
                        if path == "/admin/privacy":
                            await page.click("summary:has-text('Erase') >> nth=0")
                        else:
                            await page.click("details.convo summary >> nth=0")
                        problems[f"{path} {size}"] = await page.evaluate(A11Y_JS)
                        problems[f"{path} {size} sideways"] = await page.evaluate(
                            "document.documentElement.scrollWidth > window.innerWidth + 1 ? 'scrolls sideways' : ''")
                        await page.screenshot(path=str(SHOTS / f"{size}-{shot}.png"), full_page=True)
                await browser.close()
            return problems

        problems = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})

    def test_number_problem_on_a_phone(self):
        """[MA-4][NF-A11Y] On a phone, a number problem has a decimal keypad box and symbol buttons that type at the
        cursor and save the answer; the page passes the accessibility checks; the teacher's draft view passes too."""
        school = self.make_school()
        aid = self.open_assignment(school, kind="numeric", topics=("11.8",), count=2)
        pin = school.students[0]["pin"]
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await (await browser.new_context(**PHONE)).new_page()
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(base + f"/student/assignments/{aid}")
                await page.wait_for_selector("input[data-answer]")
                problems = {"attempt": await page.evaluate(A11Y_JS)}
                box = page.locator("input[data-answer]").first
                await box.fill("120 N/m")
                await page.click("[data-sym='²'] >> nth=0")
                value = await box.input_value()
                await page.wait_for_timeout(1200)
                await page.screenshot(path=str(SHOTS / "phone-number-problem.png"), full_page=True)
                lap = await (await browser.new_context(**LAPTOP)).new_page()
                await lap.goto(base + "/login")
                await lap.fill("#email", "rao@sun.test")
                await lap.fill("#password", PASSWORD)
                await lap.click("button[type=submit]")
                await lap.wait_for_load_state()
                await lap.goto(base + f"/teacher/assignments/{aid}")
                problems["draft"] = await lap.evaluate(A11Y_JS)
                await lap.screenshot(path=str(SHOTS / "laptop-number-draft.png"), full_page=True)
                await browser.close()
                return value, problems

        value, problems = self.run_async(flow())
        self.assertEqual(value, "120 N/m²")
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        with self.db.tenant(school.inst) as tx:
            saved = tx.val("SELECT r.answer->>'text' FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE t.assignment_id=%s", [aid])
        self.assertEqual(saved, "120 N/m²")

    def test_paper_work_pages(self):
        """[PW-4][NF-A11Y] The Paper work list and the check-and-mark page (photos beside the answers) pass the
        accessibility checks on a laptop and a phone."""
        import io

        from novaquest.services import paper
        from tests.test_paper import PNG

        school = self.make_school(n_students=3)
        aid = self.open_assignment(school, kind="quiz", topics=("8.3", "8.5"), count=2)
        sid = paper.upload(self.db, school.teacher, aid, school.student(0).user_id, [("p.png", io.BytesIO(PNG))],
                           paper.storage_dir(self.settings.data_dir), claude=False)
        base = self.live.url

        async def flow():
            problems = {}
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                for size, opts in (("laptop", LAPTOP), ("phone", PHONE)):
                    page = await (await browser.new_context(**opts)).new_page()
                    await page.goto(base + "/login")
                    await page.fill("#email", "rao@sun.test")
                    await page.fill("#password", PASSWORD)
                    await page.click("button[type=submit]")
                    await page.wait_for_load_state()
                    for path, shot in ((f"/teacher/assignments/{aid}/paper", "paper"), (f"/teacher/paper/{sid}", "paper-check")):
                        await page.goto(base + path)
                        problems[f"{path} {size}"] = await page.evaluate(A11Y_JS)
                        problems[f"{path} {size} sideways"] = await page.evaluate(
                            "document.documentElement.scrollWidth > window.innerWidth + 1 ? 'scrolls sideways' : ''")
                        await page.screenshot(path=str(SHOTS / f"{size}-{shot}.png"), full_page=True)
                await browser.close()
            return problems

        problems = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})

    def test_student_screens_in_hindi_on_a_phone(self):
        """[LG-1][NF-A11Y] A student switches to Hindi on Me: the page language is set for screen readers, Home and Me
        are in Hindi, and both pass the accessibility checks on a phone."""
        school = self.make_school()
        pin = school.students[0]["pin"]
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await (await browser.new_context(**PHONE)).new_page()
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(base + "/student/progress")
                await page.click("#lang-h summary")  # 1.31.0: language sits folded under Settings
                await page.check("input[name=lang][value=hi]")
                await page.click("#lang-h form button[type=submit]")
                await page.wait_for_load_state()
                problems = {"me": await page.evaluate(A11Y_JS)}
                await page.screenshot(path=str(SHOTS / "phone-me-hindi.png"), full_page=True)
                await page.goto(base + "/student")
                problems["home"] = await page.evaluate(A11Y_JS)
                html_lang = await page.evaluate("document.documentElement.lang")
                tab = await page.text_content(".tabbar li:first-child a")
                await page.screenshot(path=str(SHOTS / "phone-home-hindi.png"), full_page=True)
                await browser.close()
                return problems, html_lang, tab

        problems, html_lang, tab = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        self.assertEqual(html_lang, "hi-IN")
        self.assertEqual(tab.strip(), "होम")

    VOICE_STUB = """
      window.__said = []; window.__heard = 0;
      window.SpeechSynthesisUtterance = function (t) { this.text = t; };
      Object.defineProperty(window, "speechSynthesis", { configurable: true, value: {
        speak: function (u) { window.__said.push({ text: u.text, lang: u.lang }); setTimeout(function () { u.onend && u.onend(); }, 30); },
        cancel: function () {}, getVoices: function () { return []; } } });
      window.SpeechRecognition = function () { var r = this;
        this.start = function () { window.__heard++; if (window.__heard > 1) return;
          setTimeout(function () { r.onresult && r.onresult({ results: [[{ transcript: "Only plant cells have a cell wall. Animal cells like cheek cells only have a membrane." }]] });
                                   r.onend && r.onend(); }, 50); };
        this.abort = function () {}; };
    """

    def test_read_aloud_and_talk_with_nova(self):
        """[VO-1][VO-2][NF-A11Y] With the phone's own speech: 🔊 reads a question and its options aloud in the page's
        language (hi-IN for a Hindi student); 'Talk with NOVA' reads NOVA's message, listens, sends what the student
        said, and reads NOVA's reply. The buttons are hidden where the browser can't speak, and the pages still pass
        the accessibility checks."""
        from novaquest.services import language

        school = self.make_school()
        aid = self.open_assignment(school, kind="quiz", topics=("8.5",), count=2)
        pin = school.students[0]["pin"]
        base = self.live.url

        async def flow():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                ctx = await browser.new_context(**PHONE)
                await ctx.add_init_script(self.VOICE_STUB)
                page = await ctx.new_page()
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(base + f"/student/assignments/{aid}")
                await page.wait_for_selector(".say-btn:visible")
                problems = {"attempt": await page.evaluate(A11Y_JS)}
                await page.click(".qcard:not([hidden]) .say-btn")
                await page.wait_for_timeout(100)
                said = await page.evaluate("window.__said")
                await page.goto(base + f"/student/teach/{school.topics['8.5']}")
                await page.click("[data-talk]")
                await page.wait_for_function("window.__said.length >= 1", timeout=8000)
                await page.wait_for_selector("#chat .msg.me", timeout=8000)
                try:
                    await page.wait_for_function("window.__said.length >= 2", timeout=15000)
                except Exception:
                    raise AssertionError("NOVA's reply wasn't read aloud. Chat: " + (await page.text_content("#chat") or "")
                                         + " | said: " + str(await page.evaluate("window.__said")))
                problems["teach"] = await page.evaluate(A11Y_JS)
                talk = await page.evaluate("window.__said")
                me = await page.text_content("#chat .msg.me")
                mute = await browser.new_context(**PHONE, storage_state=await ctx.storage_state())
                await mute.add_init_script('Object.defineProperty(window, "speechSynthesis", { configurable: true, value: undefined });')
                mp = await mute.new_page()
                await mp.goto(base + f"/student/assignments/{aid}")
                problems["no speech: buttons hidden"] = "" if await mp.locator(".say-btn:visible").count() == 0 else "shown"
                await browser.close()
                return problems, said, talk, me

        problems, said, talk, me = self.run_async(flow())
        self.assertEqual({k: v for k, v in problems.items() if v}, {})
        self.assertEqual(said[0]["lang"], "en-IN")
        self.assertIn("A. ", said[0]["text"])
        self.assertIn("I've been reading about", talk[0]["text"], "NOVA's opening message read aloud")
        self.assertIn("Only plant cells have a cell wall", me, "what the student said was sent")
        self.assertTrue(talk[1]["text"], "NOVA's reply read aloud")
        with self.db.tenant(school.inst) as tx:
            language.set_lang(tx, school.student(0), "hi")

        async def hindi():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                ctx = await browser.new_context(**PHONE)
                await ctx.add_init_script(self.VOICE_STUB)
                page = await ctx.new_page()
                await page.goto(base + "/login/student")
                await page.fill("#school", "SUN")
                await page.fill("#klass", "8B")
                await page.fill("#roll", "1")
                await page.fill("#pin", pin)
                await page.click("button[type=submit]")
                await page.wait_for_url("**/student")
                await page.goto(base + f"/student/assignments/{aid}")
                await page.click(".qcard:not([hidden]) .say-btn")
                await page.wait_for_timeout(100)
                said = await page.evaluate("window.__said")
                await browser.close()
                return said

        self.assertEqual(self.run_async(hindi())[0]["lang"], "hi-IN")
