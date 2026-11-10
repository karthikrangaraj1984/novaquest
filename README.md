# NovaQuest: learn your school's books, and AI through them

*Where students outsmart AI.* Students learn each topic in pictures and with an AI tutor, prove it by teaching NOVA (an AI classmate full of mix-ups), and complete teacher-approved AI assignments. Teachers create syllabus-grounded assignments in minutes; principals see how every class is learning.

Each school uploads its own books for any board, subject and grade (CBSE, NCERT's current books, ICSE, IGCSE, state boards); NovaQuest reads them, finds their chapters and topics, and with Claude drafts lessons and escape rooms for the admin to approve. (The NCERT Class 8 Science book of 2018 that earlier versions shipped with is out of date and was retired in 1.12.0.) It runs on one local machine; the only cloud service is the Claude API, and without a key it runs fully offline with a built-in engine.

**Test status:** 202 automated tests, all 91 in-scope requirements passing (plus the Windows installer's own unit tests). See `TEST_RESULTS.md`.

## What runs where

| Part | Technology | Notes |
|---|---|---|
| Web app (all roles) | Python 3.11+, Starlette + Uvicorn, Jinja2 server-rendered pages, installable PWA | Phone-first for students, desktop workspace for staff |
| Database | PostgreSQL 16 + pgvector 0.8 + ltree | Row-level security keeps each school's data separate |
| Syllabus search | Postgres full-text search, fused with vector search when embeddings exist | Every answer cites chapter, section and page |
| AI | Claude API (fast tier for hints and NOVA, strong tier for questions and marking), prompt caching | Offline engine as fallback and for demos without internet |
| Embeddings (optional) | Ollama `nomic-embed-text` (768-d) | `python -m tools.nq embed` |
| Background jobs | Worker process, Postgres queue with LISTEN/NOTIFY and SKIP LOCKED | Deadlines, reminders, digests, expiry summaries, email |
| Email | Mailpit (local catcher, web UI on :8025) | Swap for a real SMTP server later |
| HTTPS | Caddy with a local certificate authority | Needed for phones to install the app |

## Quick start (Windows 10/11)

1. **Unzip** `novaquest-1.9.0.zip` somewhere in your user folder (for example `Documents\NovaQuest`).
2. **Run `NovaQuest-Prerequisites-Setup.exe`** from the unzipped folder. Windows SmartScreen may say "Windows protected your PC" because the installer isn't code-signed yet: choose **More info → Run anyway**. It installs, into `%LOCALAPPDATA%\NovaQuest`:
   - Microsoft Visual C++ runtime (Windows asks for permission once, only if it's missing)
   - Python 3.13 for your user (skipped if Python 3.11+ is already installed)
   - PostgreSQL 16 as portable binaries (no Windows service; NovaQuest starts and stops its own database on 127.0.0.1:54329)
   - Mailpit, Caddy and Poppler (`pdftotext`)
   - Tesseract OCR, which reads scanned books (installed for all users, so Windows asks once for permission), and its English language file, kept in `%LOCALAPPDATA%\NovaQuest\tessdata`
   - then, in the NovaQuest folder: a `.venv` with the Python packages, `.env` with the right paths and a fresh secret, and an `nq` command

   Every download is checked: GitHub files against the sha256 GitHub publishes, Microsoft and Python installers by their digital signature. The log is `%LOCALAPPDATA%\NovaQuest\install.log`. Run it again at any time: finished steps are skipped. `-check` only reports what's installed.
3. **pgvector (optional)**: there is no ready-made Windows build. NovaQuest works without it (full-text search). To add embedding search, run `NovaQuest-Prerequisites-Setup.exe -with-pgvector`: it installs Microsoft C++ Build Tools (about 5 GB) and compiles the bundled pgvector 0.8.0. Then run `nq init` again.
4. In a new Command Prompt in the NovaQuest folder: `nq set-key` (optional, your Claude API key), `nq init` (once, before the first `nq up`), `nq seed` or `nq demo` (optional demo schools; run after `nq up`), `nq up`, then open https://localhost:8443. `nq down` stops everything. There is no NovaQuest .exe to open: it runs in your browser, and `nq.cmd` (made by the setup) starts and stops it.
   **Upgrading from an earlier version:** `nq down` in the old folder, unzip the new version, copy your `.env` across, then `nq up`. It applies any database updates itself. Uploaded books are kept in `%LOCALAPPDATA%\NovaQuest\data` (next to the database), so they carry over; `nq up` also copies any still in an earlier version's `var\books` folder next to the new one. On Linux or macOS set `NQ_DATA_DIR` to a folder outside the version folder.

The browser will warn about the certificate until you trust Caddy's local certificate authority: with NovaQuest running, run `"%LOCALAPPDATA%\NovaQuest\bin\caddy.exe" trust` once and accept the prompt.

## Quick start (Linux or macOS)

1. **Install prerequisites**
   - Python 3.11 or later, PostgreSQL 16 with the pgvector extension (e.g. `apt install postgresql-16 postgresql-16-pgvector`, or `brew install postgresql@16 pgvector`), `poppler-utils` (for `pdftotext`; optional, but extraction is cleaner with it), and `tesseract-ocr` with English (`tesseract-ocr-eng`) to read scanned books.
   - Mailpit and Caddy single binaries from their GitHub releases, placed in `./bin` (or anywhere on your PATH).
2. **Install Python packages**: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
3. **Configure**: `cp .env.example .env` and check the database URLs. To use Claude, run `python -m tools.nq set-key` and paste your API key when asked (the secret value starting `sk-ant-`, not the key's name). It is typed hidden, saved only in `.env` (readable only by you), and a random `NQ_SECRET_KEY` is filled in at the same time. Then run `python -m tools.smoke_claude` to check the live connection.
4. **Set up**: `python -m tools.nq init` creates the database roles and schema. Then sign in as a school admin and upload your books under Books.
5. **Demo data (optional)**: `python -m tools.nq seed` prints logins for a small demo school, its staff and 12 students; the demo school uploads a small original demo book (two chapters) like any school's book. `python -m tools.nq demo` adds a full demo school (Green Valley, code GVS-DEMO: five classes, three teachers, a principal, an admin, timetables and eight weeks of data) and saves the sign-ins to `var/demo-school-logins.txt`.
7. **Start**: `python -m tools.nq up`, then open https://localhost:8443. `python -m tools.nq status` shows what's running; `python -m tools.nq down` stops everything.

With Docker instead: `deploy/docker-compose.yml` starts the same services. It is provided for convenience but was **not tested**: the build workspace had no Docker engine.

### Phones on the classroom Wi-Fi

1. Set `NQ_LAN_SITE=<this machine's IP>:8443` in `.env` and restart with `python -m tools.nq up`.
2. Install Caddy's root certificate on each phone once. The file is in Caddy's data folder: `pki/authorities/local/root.crt`.
3. Students open `https://<IP>:8443` and choose "Add to Home screen".

Answers autosave and queue on the phone if the Wi-Fi drops.

### Check the live Claude connection

`python -m tools.smoke_claude` (after `python -m tools.nq set-key`) makes one call for each AI task and checks each answer. It reports the model, tokens and cache hits, and fails if any task had to fall back to the offline engine.

## Using it

| Who | Signs in with | Main screens |
|---|---|---|
| Student | School code, class, roll number and PIN, from the login slip | Home (one 'Up next'), Learn (each topic is a 4-step path: Learn in pictures → Practise → Prove it → Teach NOVA), Tasks, Me |
| Teacher | Email and password, from the invite link | Home (to-do list), Assignments (each shows its stage and one next action; Exam papers: your pattern and difficulty, three sets to choose from, printable PDF and answer key), Classes (Students, with a page per student; Progress, with a student × topic grid and revision for chosen students), Syllabus |
| Principal / HOD | Email and password | Overview of all classes, with CSV and PDF export |
| School admin | Email and password | Classes and their subjects, staff invites, teacher mapping, year rollover, Books (upload your own textbooks) |
| NovaQuest admin | Email and password | Institutions, syllabus versions, AI usage and cost |

A typical demo:

1. The teacher creates a mission on Chapter 8, approves the questions and schedules it.
2. A student does the mission on a phone, asking for hints along the way.
3. The student teaches NOVA about parts of the cell.
4. The teacher checks the flagged answers and releases results.
5. The student sees the scorecard.
6. The principal opens the overview.
7. The teacher makes an exam paper (Assignments → Exam papers), compares Sets A, B and C, chooses one and prints it with its answer key.

## Developing

- Coding standards: `CODING_STANDARDS.md` (reusable for the PSG track and future services).
- Tests: `python -m tools.run_tests` runs all tests. They need a Postgres superuser URL in `NQ_TEST_ADMIN_URL` (default `postgresql://postgres@127.0.0.1:54329/postgres`), and Playwright Chromium for the browser tests. Results are written to `TEST_RESULTS.md` and `test_results.json`, with every test mapped to its requirement IDs.
- Release: `python -m tools.release` runs everything and builds `dist/novaquest-<version>.zip` only if all tests and requirements pass.

Project layout:

```
novaquest/core      config, clock, database + row-level security, migrations, security, audit
novaquest/ai        provider interface, Claude engine, offline engine, guardrails, embeddings, usage caps
novaquest/services  one module per product area (students, questions, assignments, attempts, review, learn, teach, dashboards…)
novaquest/web       routes, templates, static (CSS tokens, app.js, service worker)
novaquest/worker.py background jobs
data/syllabus       NCERT Grade 8 syllabus map: chapters, topics, outcomes, key terms, misconceptions, lessons
installer/windows   Windows prerequisites installer (Go, standard library only; built by tools/build_installer.py)
tools/              launcher (nq.py), seed, test runner, release, live Claude check
tests/              unit, integration, HTTP, browser, performance and full-stack tests
```

## Your school's own books (CBSE, ICSE, IGCSE, state boards…)

School admins add books on **Books**:

1. **Upload**: choose the board, subject and grade, then the PDF: the whole book, or one PDF per chapter (any names, up to 40 files). A scanned book (pages that are pictures, with no selectable text) is read with Tesseract OCR: the book page shows "page n of N" while it reads, about 2–4 seconds a page, a few pages at a time. Each page's text is saved, so a restarted worker carries on where it stopped. Diagram labels read less well than the main text, so check the chapter and topic titles before publishing. A whole book with no "Chapter N" headings is split at its numbered sections ("1.1 Photosynthesis", "2.1 …"), as Cambridge books print them.
2. **NovaQuest reads it** in the background (a minute or two): it finds the chapters ("Chapter N" headings, or one per file) and each chapter's topics. With a Claude API key Claude drafts the topics and their learning outcomes; without one, the book's own headings are used (numbered sections like "2.3 …", or ALL-CAPS headings). The text is cut into passages that cite the book and page.
3. **Check and publish**: rename chapters and topics, set outcomes, remove a topic that isn't really one (its text joins its neighbour). Students see nothing until you publish.
4. **Give it to classes** on School setup → Subjects (a class can study several books), and map its teachers per subject.
6. **Escape rooms (needs Claude)**: once the book is published, "Draft escape rooms with Claude" writes one room per chapter: a story and four locks, each joining the chapter's science with an AI skill (sorting by clues, clean training data, checking AI claims, pattern or cause, learning from data, predictions). NovaQuest checks every lock (a sorting lock must be solvable, and the clues chosen must matter; a draft that fails goes back to Claude once with the reason). Preview each room with its answers and approve it; then students play it from Learn and Escape rooms, and teachers see it on the class's Escape rooms page.
5. **Lessons (needs Claude)**: "Draft lessons with Claude" writes each topic's picture lesson (flows, comparisons, cycles, groups, layers), quick checks and NOVA's mix-ups, from the book's own text. Each part is validated, then waits for your approval (answers shown). Until then students still have practice, the understanding check, Ask NOVA and the textbook text.

Each school's books, topics, passages and lessons are private to that school (enforced by the database). Upload only books your school has the right to use: NCERT books are free to use; most ICSE and IGCSE textbooks belong to their publishers.

## Book pages

Every topic page has **Book pages**: the topic's real textbook pages as images, with all the figures and tables as printed (the extracted text alone loses them). The admin turns them on under Books → the book → "Book pages for students", confirming the school has the right to show the full pages. Pages are rendered with Poppler's `pdftoppm` (installed by the Windows installer) and cached in `var/pagecache`.

## Learn in pictures

Every one of the 125 topics has a picture-first lesson (419 picture cards): the idea in simple words, one picture at a time, ending with "the crux" in three points and a picture quiz. Students open it from Learn → Learn in pictures, or from any topic page.

- Pictures are original NovaQuest drawings (46 labelled science diagrams: cells, microbes, flame, eye, ear, refinery, electroplating, solar system and more) plus flows, cycles, comparison tables, groups, real-data bar charts and layer diagrams. No textbook images are copied.
- On labelled drawings, tapping a name lights up its part, and "Test yourself: hide the names" turns the drawing into a quiz.
- Quiz answers are checked on the server and count towards mastery; finishing a lesson is recorded, and teachers see how many students finished it on the syllabus board. Teachers and leaders can preview any lesson with its answers.
- Content lives in `data/pictures/chNN.json` (one file per chapter) and drawings in `novaquest/web/scenes/`. Every file is validated when the app starts and by the tests, so a broken card never reaches students. Have a science teacher review the lessons before use with students.

## Known limits in 1.3

- PSG MSc track (coding sandbox, similarity checks): next release.
- Full guided lessons exist for Chapter 8 (the demo chapter). Other topics teach from textbook extracts, with the tutor, practice and understanding checks.
- School books: picture lessons use NovaQuest's data-drawn kinds (flows, comparisons, groups…); the labelled science drawings exist only for the NCERT book. Topic-finding without Claude depends on the book's headings, so check the topics before publishing.
- Before real Grade 8 students use it: confirm Claude data terms meet the DPDP Act (or move to Claude on Bedrock India), and have a lawyer review the consent process.
