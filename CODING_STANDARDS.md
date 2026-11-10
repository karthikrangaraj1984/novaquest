# NovaQuest coding standards

These standards apply to every NovaQuest codebase (the Grade 8 app now, the PSG MSc track later, and any new service). They are written to be reused: copy this file into a new repository and follow it from the first commit.

Rules marked **must** are checked in code review and, where possible, by the test suite. Rules marked **should** are the default; break one only with a comment explaining why.

---

## 1. Language and layout

- **Must** target Python 3.11 or later. Use the standard library first; add a dependency only when it removes real complexity, and pin its exact version in `requirements.txt`.
- **Must** follow PEP 8, with a 100-character line limit.
- **Must** keep the package layout:

  | Folder | Holds | May import |
  |---|---|---|
  | `novaquest/core/` | config, clock, database, security, audit | standard library only |
  | `novaquest/ai/` | model providers, prompts, guardrails | `core` |
  | `novaquest/services/` | business rules, one module per product module | `core`, `ai`, other services |
  | `novaquest/web/` | routes, templates, static files | `services`, `core` |
  | `tests/` | unit, integration and end-to-end tests | anything |
  | `tools/` | command-line scripts (seed, ingest, test runner, release) | anything |

- **Must not** put business rules in routes or templates. A route parses input, calls one service function, and renders the result.
- **Must not** import from `web` inside `services`, or from `services` inside `core` (no upward imports).

## 2. Naming

- Modules and functions: `snake_case`; classes: `PascalCase`; constants: `UPPER_SNAKE_CASE`.
- Function names start with a verb: `create_assignment`, `release_results`, `compute_mastery`.
- Booleans read as questions: `is_open`, `has_consent`, `can_review`.
- Database tables are plural nouns (`assignments`); columns are `snake_case`; foreign keys end in `_id`; timestamps end in `_at` and are stored as UTC ISO-8601 strings.
- Use the product's words in user-facing text: "progress" and "mastery", never "marks" or "grade" (AI scores are not official marks).

## 3. Functions and types

- **Must** add type hints to every public function signature.
- **Should** keep functions under 40 lines and with no more than 5 parameters; group related parameters in a dataclass.
- **Must** use `@dataclass(frozen=True, slots=True)` for value objects passed between layers. `slots=True` keeps memory per object small.
- **Must** write a one-line docstring on every public function that says what it does, not how.
- **Must not** use mutable default arguments.

## 4. Errors

- **Must** raise the domain errors from `novaquest.core.errors` (`NotFound`, `Forbidden`, `ValidationError`, `Conflict`, `RateLimited`) from services. The web layer maps them to HTTP status codes in one place.
- **Must not** catch broad `Exception` except at a system boundary (an AI call, a background job, a request handler), and there it must log the error and fail safe.
- **Must** write user-facing error messages that say what went wrong and how to fix it: "Roll 12 is already used by Aarav S. Use a different number."

## 5. Data access and multi-tenancy

- **Must** read and write institution data only inside `db.tenant(institution_id)`, which sets the Postgres row-level-security context. Every institution-owned table has `institution_id` and a `tenant_isolation` policy, so a missing `WHERE` cannot leak another school's data. Use `db.platform()` only for NovaQuest-admin work, and never in a request made by a school user.
- **Must** connect the app as the non-owner `nq_app` role, so row-level security always applies. Migrations run as `nq_owner`.
- **Must** use parameterised SQL (`%s` placeholders with psycopg). String-formatting values into SQL is never allowed; build identifiers with `psycopg.sql`.
- **Must** add an index for every column used in a `WHERE` or `JOIN` on a table expected to grow past 1,000 rows.
- **Must** change the schema only through numbered migrations in `novaquest/core/migrations/`; never edit an applied migration.
- **Must** wrap multi-row changes in a transaction (`with db.transaction():`).
- **Must** write an audit event for logins, account changes, score overrides and result releases.

## 6. Security and privacy

- **Must** hash passwords and PINs with `hashlib.scrypt` and a per-user random salt. Never log or store a plain PIN, except to print a login slip once at creation or reset.
- **Must** protect every state-changing form or API call with the CSRF token.
- **Must** check the role and institution of the signed-in user in the service function, not only in the route.
- **Must** collect only the data the feature needs. Grade 8 students have no email or phone stored.
- **Must** set secure headers (CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`) through the shared middleware.
- **Must not** commit secrets. Read them from environment variables (`NQ_SECRET_KEY`, `ANTHROPIC_API_KEY`).

## 7. AI calls

- **Must** call models only through `novaquest.ai.provider.LLMProvider`. Business code never imports an SDK or builds an HTTP request to a model.
- **Must** ground every syllabus answer in retrieved passages and return the citation with it.
- **Must** ask for structured JSON output, validate it, and fall back safely when validation fails (for marking: flag for teacher review; for hints: give a generic nudge).
- **Must** pass every student-facing AI reply through the guardrails (`ai.guard`): content filter, topic lock, and answer-leak check during open assignments.
- **Must** record token usage per institution and enforce the daily cap before calling the model.
- **Should** use the fast model tier for live hints and the AI student's replies, and the strong tier for question generation and marking.
- **Should** put stable text (persona, rules, syllabus passages) first in the prompt and mark it cacheable, so repeated calls reuse the prompt cache.
- **Must** keep the offline provider in step with the Claude provider, so tests and demos run without a network connection.

## 8. Performance and memory

- **Must** set a time budget for every user-facing call and test it: page render under 300 ms on the server, tutor hint under 3 s, assignment generation under 60 s.
- **Must** set a timeout on every outbound network call (default 20 s) and retry only idempotent calls, with exponential backoff and jitter, at most twice.
- **Must** use the shared `psycopg_pool` connection pool (bounded `min_size`/`max_size`) and keep transactions short: one transaction per request, never held across user think-time.
- **Should** let psycopg prepare repeated statements (`prepare_threshold`), and batch inserts with `executemany`.
- **Must** wake background workers with `LISTEN/NOTIFY` rather than tight polling, and claim jobs with `FOR UPDATE SKIP LOCKED`.
- **Must** select only the columns needed; never `SELECT *` in service code.
- **Must** paginate any list that can exceed 100 rows.
- **Should** cache read-mostly data (search indexes, syllabus trees) in memory with a size-bounded LRU and an explicit invalidation key (syllabus version). Never cache per-student data in process memory.
- **Should** stream or iterate over large files (PDF ingestion, CSV upload) instead of loading them whole; cap uploads at 10 MB.
- **Should** prefer generators and `__slots__` for objects created in large numbers.
- **Must** keep pages light: server-rendered HTML, one shared CSS file, small vanilla JS, no front-end framework. Static files are compressed and cached.
- **Must not** make an AI or network call while holding a database transaction.

## 9. Front end and accessibility

- **Must** use the design tokens in `static/app.css` (colours, radii, spacing, fonts). No inline colour values in templates.
- **Must** give every input a visible or screen-reader label, every icon-only button an `aria-label`, and every page a single `h1`.
- **Must** keep touch targets at least 44 × 44 px, and text contrast at least 4.5:1.
- **Must** never rely on colour alone: pair status colours with text or an icon.
- **Must** keep the app usable by keyboard alone, with a visible focus ring.
- **Must** autosave student answers and recover them after a refresh or dropped connection.
- **Should** design for the smallest screen first (360 px wide) and test on a phone-sized viewport.

## 10. Testing

- **Must** tag every test with the requirement IDs it proves, in the docstring: `"""[ST-1] Teacher adds 40 students from one CSV."""`
- **Must** cover every requirement ID with at least one passing test. The test runner fails the release if any ID is uncovered or failing.
- **Must** use the fixed clock (`core.clock.FixedClock`) and the offline AI provider in tests, so results never depend on the time of day or the network.
- **Must** give each test its own temporary database.
- **Must** write a regression test before fixing a bug.
- **Should** keep the whole suite under 2 minutes.
- **Must** run `tools/release.py`, which runs every test, writes `TEST_RESULTS.md` and `test_results.json`, and builds the release only when everything passes.

## 11. Logging

- **Must** log in one line per event, with a request ID, user ID and institution ID, never the content of a student's answer or a PIN.
- **Must** use levels consistently: `INFO` for state changes, `WARNING` for recoverable problems (AI fallback, rate limit), `ERROR` for failures needing attention.

## 12. Commits and reviews

- One logical change per commit, with a message in the imperative mood: "Add PIN reset for teachers".
- Every change is reviewed against this file. A reviewer can block on any **must**.
- Update `CHANGELOG.md` for every user-visible change.

## 13. Installers and platform tools

- Native tools that ship to users (such as `installer/windows`) are written in Go with the standard library only, so they build offline into one static file. They must pass `gofmt`, `go vet` and `go test`.
- Every download must be verified before use: a pinned sha256 (or the digest the host publishes for that exact release), or a valid Authenticode signature from the expected publisher. Never run or unpack an unverified file.
- Pin every third-party version in one place (`versions.go`) and change it only together with a full test run.
- Steps must be idempotent: check first, install only what's missing, and say what was skipped. A failed step is reported with the log path and never hides the others.
- Never touch secrets the user set (such as `ANTHROPIC_API_KEY`); files holding secrets are restricted to their owner.
- Code that runs on several operating systems must keep platform branches small and testable (pure functions that take the platform as a parameter).

## 14. Learning content

- Lessons are data, not code: one JSON file per chapter, validated on load and in tests. A content error must fail the build, never reach a student.
- Write for the grade: short sentences (≤ 240 characters), one idea per card, everyday examples. Keep the crux to 2–4 points.
- Stay on the syllabus: facts come from the prescribed textbook. When unsure of a figure or date, leave it out rather than guess.
- Bar charts show real data only (calorific values, decibels, percentages of air). For orders of size or time with no exact numbers, use a list or flow instead.
- Pictures are original drawings or composed from emoji and shapes; never copy textbook figures. Every drawing has named label points, and every picture has a text alternative.
- Quiz answers stay on the server; explanations are sent only after the student answers.

## 15. Many books (syllabi)

- Never assume one syllabus. Find the book from the data in hand: a topic's `syllabus_id`, a class's books (`syllabus.class_syllabi`), a teacher's books for a class (`syllabus.teacher_syllabi`). `current_syllabus_id` means the shared NCERT book only.
- Tables that hold book content (`syllabi`, `syllabus_nodes`, `source_passages`, `lessons`, `misconceptions`, `picture_lessons`) carry `institution_id`: NULL for shared books, the school's id for its own. Row-level security enforces it; code must still check ownership before editing (`books.own_book`).
- Caches keyed by a book id must check visibility on every read (as `syllabus.get_tree` does), so a cached book can't leak to another school.
- AI-drafted content is stored as `draft`, validated with the same validators as shipped content, and reaches students only after an admin approves it. Invalid parts are dropped, never shown.
- Long AI work runs as background jobs in small batches that queue the next batch, so one big book never blocks reminders or other schools.
- Uploaded files are streamed to disk with size limits and a type check (`%PDF-`), never held whole in memory.
