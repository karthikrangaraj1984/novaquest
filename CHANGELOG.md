# Changelog

## 1.31.0 (28 Oct 2026): The student's own growth

From the review of the student screens.

- **Fixes:**
  - **My progress no longer breaks.** For a student who missed work whose results were released, the page showed "No submitted work for this assignment". That work is now listed as **Missed** in Tasks.
  - **One status per topic.** A topic's level (Learning, Practised, Mastered) is shown everywhere. Learn no longer says "In progress" for a topic its own page calls Mastered. A mastered topic offers **Revise: practise again** instead of step 1, and "Continue where you left off" skips mastered topics. Chapters show how many topics are practised.
  - **Menu highlight.** Results highlight Tasks in the menu.
  - **Daily 5 question quality.** Daily 5 leaves out questions that are one sentence with a word swapped.
- **Home.** In order:
  1. **Due soon**: work closing in the next two days, as "Closes today, 6:00 pm (in 5 h)". If nothing is that close, the next deadline is shown.
  2. The one learning step to do next, preferring the chapter taught in class now.
  3. Daily 5.
  4. **New results**, with ▲/▼ against the last result in that subject.
  5. My mistakes, and an exam coming up.
- **Tasks.** Every card shows its deadline. **Results** lists every result with its score and change. Released work not handed in is **Missed**.
- **My progress:**
  - **This week**: a weekly goal (topics to work on), with progress and last week's result, plus the week and Daily 5 streaks.
  - **My scores**: a score trend chart per subject with the average, the latest score and the recent change, plus best this term and most improved topic.
  - **Strong** and **Needs work** topics.
  - **Help me improve**: My mistakes, Exam mode and Ask my teacher.
  - Chapters and "More" are folded away, and **Settings** (share with parents, language, sign out) sit last.
  - Everything compares a student only with themselves.
- **My mistakes.** Every question got wrong in released work stays until it's right on a redo:
  - multiple-choice questions are redone five at a time and marked at once;
  - written answers show the strong answer and can be marked "I understand it now".
  - Redoing never changes a score.
- **Learn** opens on the chapter taught in class now. Earlier chapters and the ones coming up fold into one line each.
- **Exam mode.** A student adds an exam (subject, chapters, date) and gets:
  - a countdown and how ready they are;
  - a day-by-day revision plan of their weakest topics, ticked off as they revise, with the day before kept for practice;
  - 15-minute timed practice papers, marked at once (practice only, no levels change).
- **Ask my teacher.** From a topic, or a question they got wrong, a student writes to the teacher of that subject. The teacher sees "N questions from students" on Today and replies on one page. The student gets the reply under My questions and in the bell.
- **Reflection.** After each result: Easy / Okay / Hard, and what they'll do next (revise, redo mistakes, ask the teacher, move on). The teacher sees only the class's counts on the results page.
- **Data protection.** Exports and erasure include the new student data.
- **Database:** migration 020.

## 1.30.3 (28 Oct 2026): Figures on the charts

- On the principal's and admin's Overview, **Students active each week** and **Understanding over 8 weeks** now print each week's figure above its bar or point. Only the first, middle and last dates sit under the chart; the line of "31 Aug: 38% · 7 Sep: 47% …" under each chart is gone. Hovering a bar or point still shows its date and value, and screen readers get the full list.
- The understanding line uses the chart's height for the range of the figures, so week-to-week changes are visible.
- No database changes.

## 1.30.2 (27 Oct 2026): A book is read once, even with NovaQuest running

- **The problem.** With NovaQuest running, `nq demo` stopped with "Choose published books only". An uploaded book is read by NovaQuest's worker in the background; the demo also read it straight away. Both readers ran, the demo published the book, and the worker then finished and put the book back to "Check and publish".
- **The fix:**
  - A book now has one reader. The reader claims the book before starting and marks every step. A second reader leaves it alone, unless the first has made no progress for 30 minutes (a reader that stopped). This also protects schools whose admin presses Read again while a book is being read.
  - The demo waits for the worker to finish reading each demo book before publishing it. `nq seed` does the same.
- No database changes.

## 1.30.1 (27 Oct 2026): `nq demo` works on every install

- **The problem.** On installs without NovaQuest's ready-made NCERT Science 8 book (retired in 1.12.0), `nq demo` stopped with "No syllabus loaded yet".
- **The fix.** The demo now uses the ready-made book only if it is installed and current; otherwise it uploads a demo Science 8 book like any school's own book.
- **Safe to run again.** If setting up the demo fails half-way, the half-made school is set aside (its school code and sign-ins renamed, made inactive) so `nq demo` can simply be run again.
- No database changes.

## 1.30.0 (27 Oct 2026): A teacher's week across several classes, and the whole school at a glance

From the UX review of the teacher and admin screens.

- **Today.** The teacher's home is now **Today**. With a timetable it lists today's lessons in time order (done, now, later) with the next one highlighted. Each row shows the class, the chapter from the term plan, the period that class is on, and a **Present** button that opens exactly that period. Without a timetable it lists every class and subject the teacher teaches. To-do rows now always say the class and subject ("8B · Science"), work set for several sections together is one row ("8A 11/18, 8B 7/18 handed in"), and a teacher sees only work in the subjects they teach.
- **Weekly timetable (optional).** Teach → Timetable: pick a class and subject, tick the days, give the start time and length. A lesson that clashes with another one is refused. With a timetable:
  - lesson plans count the lessons a chapter really gets;
  - each class's place in the plan follows the lessons already taught;
  - homework closes just before the class next meets.
- **New menu: Today · Teach · Classes · Assignments.** Teach holds Lesson plans, Topics and access, Term plan and Timetable. Its class-and-subject switcher is a row of chips, and for sections of the same subject each chip says where that class is ("Period 6 next", "Ch 10 now · 4 behind").
- **One lesson plan, a position per section.** The prep pack is shared by all the sections, but each class has its own next period, marked on the plan ("Next for 8A") and on the Present button.
- **Plan once for all sections.** On Term plan, **Use these dates for your other sections** copies one class's chapter dates to the teacher's other sections in one step.
- **Set work for several sections at once.** On New assignment, **Also set it for** ticks the other sections where the teacher teaches the subject. The questions are checked and approved once. When scheduling, each ticked class gets its own opens and closes times (pre-filled from the timetable), and its own results. The results page links the sections with how many handed in.
- **Whole school at a glance** (principal and school admin, Overview):
  - **Every class, every subject**: understanding, share of the book practised, term plan status, work set and handed in, and teachers, coloured by level;
  - filters by grade and subject;
  - a summary by grade;
  - students active each week (8 weeks);
  - a teacher table: what each teaches, work set, hand-in, lessons presented and last sign-in.
- **Quick fixes:**
  - **Results and assignments:**
    - Students who haven't handed in are listed 1, 2 … 10 (not 1, 10, 11, 2).
    - Release and the class message appear only once work is handed in.
    - An open assignment opens on its results, with Questions one tab away.
    - The Assignments list filters by stage (Needs you, Open, Drafts, Done) and class.
    - Every assignment type is shown, with no "More types" fold.
  - **Pickers and layout:**
    - Pickers apply as soon as they change (no Show buttons).
    - One main button per page header.
    - Phone layout for to-do rows.
    - Initials ignore titles (PR, not MPR).
- **Demo school.** `nq demo` adds **Green Valley Public School (demo)** (school code GVS-DEMO):
  - five classes of 18 students, studying Science, Mathematics and Science 9;
  - three teachers (two with timetables), a principal and an admin;
  - term plans, eight weeks of learning, and work at every stage.
  - The sign-ins are printed and saved to `var/demo-school-logins.txt`.
- **Database:** migration 019 adds the timetable and links work set for several sections.

## 1.29.1 (27 Oct 2026): Diagrams sit beside the right key point

- **The problem.** On "What Is a Cell?", the slide "The cell wall in plant cells gives rigidity and strength to plants" showed Claude's diagram of cheek, muscle and nerve cells. Claude numbers the key point each diagram belongs to, and that number can be off by one.
- **The fix.** NovaQuest now decides where a diagram goes from what it says. The words in the diagram (headings, labels, cells, steps) are matched against each key point, leaving out words most of the topic's points share, like "cell". The cheek/muscle/nerve diagram now goes with the point about cell shapes. A diagram that matches no point, or whose point already has one, gets a slide of its own. Claude is also now asked to copy the key point's words instead of giving a number.
- This applies when the slides are made, so packs Claude already wrote are fixed without rewriting them.
- No database changes.

## 1.29.0 (27 Oct 2026): Pictures on teachers' slides

- **The book's own figures on the slides.** NovaQuest now finds the figures on the book's printed pages, with their captions ("Fig. 2.5: A schematic drawing of (a) An animal cell and (b) A plant cell"), and cuts each one out. A figure in several parts, (a) to (d), stays one figure, and a caption over two lines is joined. Page backgrounds, logos, running heads and page numbers are left out. On the projector each figure gets its own large slide right after the key point it's about. A figure on a page two topics share goes to the topic whose text mentions it ("as shown in Fig. 2.7"). On Prepare to teach, each topic shows its figures as thumbnails. Tested on NCERT Curiosity Grade 8 Chapters 2, 5 and 10: every numbered figure found, with its caption.
- **Diagrams Claude draws.** With Claude, each topic in the prep pack gets 1–2 diagrams for its key points, drawn from the book's facts: a flow, a cycle, a comparison (plant cell vs animal cell), groups, layers or a word equation. Each sits beside its key point on the slide, and appears on the prep page. A diagram that isn't drawn right is left out. Chapters Claude already wrote get diagrams after **Rewrite with Claude**.
- **The school decides about figures.** A school's own book shows its figures on teachers' slides only after the admin turns on **"The book's figures on teachers' slides"** on the book's page (Books → the book) and confirms teachers may show them in class. This is separate from showing the full pages to students. The shared NCERT book, and books whose full pages are already shown, have figures on. Until it's turned on, the prep page says how.
- **Fast pages.** Figures are cut out in the background when the setting is turned on (a minute or two per book), and kept. Until then, the prep page says it's preparing them, and pages never wait. Figure images are only for signed-in staff and are never kept by the browser, like the rest of the presentation.
- No database changes.

## 1.28.1 (27 Oct 2026): Claude's prep packs no longer get cut off

- **The problem.** For a long chapter (Curiosity Chapter 2), Claude wrote the whole prep pack in one reply, the reply got too long and was cut off, and the page showed "bad JSON: Unterminated string … column 18647" with the book's pack instead.
- **The fix.** Claude now writes the pack a piece at a time: the overview, mix-ups and key terms; then two topics at a time; then four of the book's activities at a time (plus up to two demos of its own). Each reply is short, so it isn't cut off. The chapter's text is sent once and reused across the pieces (prompt caching), so the cost stays about the same. A reply that is cut off anyway is noticed, and that piece comes from the book's pack (the page says which pieces); the pack fails only if no topic comes through.
- **Plain messages.** Failures now read "Claude's reply wasn't complete", "Claude's reply was too long and got cut off" or "Claude couldn't be reached (busy, offline or the daily limit)", never the technical detail. A chapter that failed before shows the plain message; click **Try again** to have Claude write it with the new method. If a rewrite fails, the last pack Claude wrote stays in use.
- No database changes.

## 1.28.0 (27 Oct 2026): Open chapters, and escape rooms as assignments

- **Open whole chapters.** On Syllabus, each chapter shows whether it's open, locked or partly open, with **Open chapter** / **Lock chapter** (one click for all its topics). **Open only some chapters** lists the chapters with tick boxes: tick the ones the class should see, and the rest are locked in one step.
- **How a book starts for a class.** Under "What students can study on their own", choose:
  - **Every chapter open** (as before);
  - **Closed until I open chapters**: students see nothing on Learn until you open a chapter;
  - **Open each chapter on its term-plan start date**: chapters open by themselves on the start dates in your term plan, and chapters without dates stay closed.

  A chapter you open or lock by hand always wins over the term plan. For a book in parts, the setting covers every part. Work you set is still always open to the students you set it for.
- **Escape rooms follow their chapter.** A chapter's escape room shows on Learn only while the chapter is open for the class (before, every approved room was always open).
- **Escape rooms as assignments.** In New assignment, the type **Escape room** sets the room of the chapter you pick; or use **Set as assignment** next to each room on Classes → Escape rooms. Choose the whole class, a group or chosen students, and the open and close dates, like any assignment. There are no questions to check: the room is ready to schedule. Students see it in Tasks (with the usual reminders); opening it takes them straight into the room, even if the chapter is closed for self-study. Each lock they open is saved as progress; escaping hands it in at 100%, and at the deadline the share of locks opened counts. A student who had already escaped the room before it was set gets it handed in straight away. A chapter without an approved room can't be set.
- Database: migration 018 adds each class's self-study setting per book and the "escape" assignment type.

## 1.27.0 (27 Oct 2026): Present a chapter on the projector

- **Present on the projector.** On Prepare to teach, **Present on the projector** (and **Present** on each period of the lesson plan) opens the chapter as slides, full screen, one period at a time. Slides follow the lesson plan:
  - a cover;
  - for each topic, the opening question for the class (the teacher's hook instructions go in the speaker notes);
  - the key points, one per slide in large type;
  - the school's approved picture-lesson cards with their diagrams;
  - the textbook's own pages (shared books, and school books whose admin has turned on full pages);
  - the book's activity with what to get ready, the steps and the safety note;
  - a check question whose answer appears on a click;
  - "What we learnt".

  The activities period shows each of the book's activities; the review period asks "True or false?" about each mix-up, then the key words and a quiz.
- **Teaching controls.** Back/Next buttons, arrow keys or a presentation clicker; A or Enter shows the answer; F for full screen; a period picker. **Presenter view** opens a second window for the teacher's laptop screen (with the projector as a second display): the current slide, its speaker notes (what to ask, what to listen for, the mix-ups), the next slide, and a clock for the period with the time for the current part (hook, key points, activity, check, wrap up). The two windows stay in step.
- **Only inside NovaQuest.** There is no download or export of the slides (no PDF, PowerPoint or images). The page asks the browser not to keep or index it, printing shows only "This presentation can only be shown inside NovaQuest", and right-click saving, dragging images out, copying and Ctrl+S/Ctrl+P are turned off. Every slide carries a faint watermark with the school, the teacher and the date, and each time a deck is opened it is recorded in the audit log. Textbook page images on slides are served only to signed-in staff, for that chapter's topics, and never kept by the browser. No web page can stop a phone photo or a screenshot of the screen; the watermark shows where such a copy came from.
- No database changes.

## 1.26.0 (27 Oct 2026): Prepare to teach (unit prep packs)

- **A prep pack for every chapter.** Syllabus now has a **Prepare to teach** tab (also a "Prepare" link on each chapter of the term plan and of the Syllabus page). It opens the chapter in class now and gives the teacher:
  - **A lesson plan by period.** Chapters are shared over the periods you have: by default the school days your term plan gives the chapter, or choose the number of periods and their length (30–60 minutes). Each period has a timed flow (hook, teach, activity, check, wrap up). Fewer periods than topics puts neighbouring topics together; more adds a period for the book's activities, a review-and-quiz period and practice periods.
  - **For each topic:** what students will be able to do, an opening hook, 3–5 key points from the book, the textbook pages, the book's activity for it, a check question (or the topic's question asked again at the end, with what to listen for) and the mix-ups to watch for.
  - **Mix-ups to watch for:** what a student might say, the right idea, how you'll notice it and how to clear it up, plus the ones your class has already shown in its answers.
  - **The book's activities written up as demos:** each "Activity 2.2: Let us study a cell" box with whether the book makes it a teacher demonstration, what to get ready, the steps, the questions to ask and the book's safety notes (tested on all 13 chapters of NCERT Curiosity Grade 8: 89 of its 90 activities found; Activity 2.1 sits before the chapter's first topic, which NovaQuest doesn't store).
  - **Key terms**, **my notes** (private to each teacher, printed in their PDF), **Download PDF**, and **Make the opening quiz**: five easy questions across the chapter as a draft assignment, to find out what the class already knows or to show on the projector.
- **With Claude**, the first time a teacher opens a chapter Claude writes a fuller pack in the background (a minute or two; the page refreshes itself) and it is kept for every teacher in the school: hooks, key points, a multiple-choice check per topic, mix-ups with fixes, the activities with materials and questions, and term meanings. Claude is told to use only the book and to add at most two demos of its own with safe everyday materials (no flames, chemicals, mains electricity or tasting). Its pack is checked against the book: topics it invented and broken check questions are dropped, and none of the book's activities is lost. **Rewrite with Claude** asks again; if Claude fails, the last good pack stays.
- **Without Claude**, the pack is built from the book in a fraction of a second (outcomes, approved lessons, the book's activity boxes and the mix-ups drafted for the book); nothing is stored.
- Database: migration 017 adds unit packs (one per school and chapter) and teachers' chapter notes.

## 1.25.0 (26 Oct 2026): Books in parts are one subject

- **Upload each part on its own, see one subject.** On Books, the upload form has a new **Part** choice (Whole book, Part 1–4). Upload NCERT Ganita Prakash Part 1 and Part 2 with the same board, subject and grade, and NovaQuest treats them as one Mathematics subject. If you leave Part blank, NovaQuest reads "Part-I" / "Part-II" from the title or the cover pages (both Ganita Prakash parts are recognised). You can change it on the book's page ("Part of a book in parts").
- **School setup: one tick.** The class's subject list shows "Mathematics · NCERT Grade 8 (Part 1 + 2)" once; ticking it links both parts. Mapping a teacher to the subject maps every part. A part published later (Part 2 in the second term) joins the classes and teachers that already have Part 1, with no extra setup.
- **Students** see one Mathematics subject: Learn lists Part 1's chapters, then Part 2's ("Part 2 · 1. Fractions in Disguise"), and tasks from either part share one subject tag and filter. Daily 5, Ask NOVA and escape rooms already covered every book of the class.
- **Teachers** pick the subject once. Topic lists in New assignment, New exam paper, Syllabus, the term plan, class progress and a student's page show both parts, with topics marked "P2·1.1". An assignment or paper can mix topics from both parts (each question stays with its own part); the start-of-term check, progress checks and the growth report cover the whole book. Principals' class pages and briefings follow the same subject.
- Database: migration 016 adds a part number to books (blank for a whole book). Books already uploaded are unchanged until you set a part.

## 1.24.2 (26 Oct 2026): Chapter titles from the right contents page

- **Two-part books.** NCERT Ganita Prakash Grade 8 comes in two parts, each with its own front matter (hegp1ps.pdf, hegp2ps.pdf) and its own Chapters 1–7. When both parts were uploaded as one book (or Part 1 with Part 2's front matter), chapter titles were taken from a contents page by number alone, so Part 1's Chapter 1 (A Square and A Cube) was titled from Part 2's Chapter 1. Each contents title now goes to the chapter whose first page prints it; a contents page is used by number only when it matches most chapters by their words. Uploading both parts as one book gives all 14 chapters their right titles (Part 2's chapters become 8–14).
- **Contents pages.** Titles are read from the "Contents" heading on, not from the "About the Book" sentences before it (one chapter was titled "'Fractions in Disguise' introduces percentages and several"). A title ending in a number keeps it ("Proportional Reasoning-1"; it lost "-1" before).
- A book already uploaded keeps its titles: rename the chapters on the book's page ("Rename chapter"), or delete and upload it again.

## 1.24.1 (26 Oct 2026): Maths long answers state the problem first

- In a two-part maths long answer (written without Claude), part (a) asked "Which rule or method do you need for this problem, and why?" before the problem appeared in part (b). The problem now comes first, in part (a): "(a) A parallelogram has a base of 12 cm and a height of 11 cm. What is its area? Which rule or method do you need to solve this, and why? (b) Now solve it, showing your working." Same in assignments, papers and answer keys.

## 1.24.0 (26 Oct 2026): Maths books (tested with NCERT Ganita Prakash Grade 8, Parts 1 and 2)

- **Maths is read correctly from the PDF.** For a book whose subject is Maths/Mathematics/Ganit, NovaQuest reads where each word sits on the page, so powers keep their place (1² was read as "12", 10⁴ as "104", 0.001 × 2⁴ as "0.001 × 24") and fractions printed one number above another stay in the line (9 × 1/10 was read as "9 × 1"; d = bc/a as "d = bc"). Negative powers (10⁻³) too. Everything built from the book (lessons, questions, NOVA, escape rooms) now sees the right maths. Science and other books are read exactly as before.
- **Chapter titles and topics.** Titles printed over several lines in capitals are joined ("A Story of Numbers", "We Distribute, Yet Things Multiply"), a hyphen at a line break joins ("The Baudhāyana-Pythagoras Theorem"), and a figure label is no longer glued onto a heading ("Kite and Trapezium"). A maths chapter that numbers only its first section uses its bold headings as topics: Ganita Prakash Part 2's Area chapter now has Triangles, Parallelogram, Rhombus, Trapezium and more, instead of one topic.
- **Maths questions without Claude.** NovaQuest's ready-made problems now cover every Grade 8 chapter: squares and square roots, cubes and cube roots, powers and the laws of exponents, powers of 10, quadrilateral angles, divisibility (remainders by digit sums), the distributive property and identities, ratios and sharing, proportion and unit price, inverse proportion, maps and scale, unit conversion, percentages and discounts, Pythagoras, areas of parallelograms, trapeziums and rhombuses, means, and "think of a number" algebra. For a maths book they're used for:
  - **Quizzes, missions and Daily 5:** multiple choice whose wrong options are the usual slips (210 or 42 for 21², ₹2520 instead of ₹252).
  - **Short answers:** "… Show your working.", marked on the method's steps and the final answer.
  - **Long answers:** (a) which method and why, (b) solve it with working.
  Without Claude, maths quizzes were sentence questions ("Which statement about square numbers is correct?"). With Claude, questions are still written from the book itself.
- **Exam papers for maths.** A new section type, **Number problems (show working)**; in a maths paper, 2–5 mark sections default to it. The answer key gives the worked solution and shares the marks between the method's steps and the correct final answer. Powers now print properly in all PDFs (the standard PDF font has no ² or ⁵; they're drawn raised), and ₹, π and √ print.
- **Claude's instructions for maths.** For a maths course, Claude is told to test reasoning and calculation (work it out, choose or justify a method, spot the mistake in a worked step), to use wrong options from typical mistakes, and to give worked solutions as model answers; escape rooms use the chapter's rules and patterns. Marking of written answers gives credit for each correct method step, even when the final answer is wrong, and accepts equivalent forms (½ = 0.5 = 50%).
- Also fixed: a percentage problem could say "130% of the students walk to school"; a unit-price problem could ask about the same number of notebooks it gave; money now reads "₹240", not "240 ₹".
- No database changes.

## 1.23.0 (26 Oct 2026): Voice: read aloud and talk with NOVA

- **🔊 Read aloud.** Every assignment question and Daily 5 question has a 🔊 button that reads the question and its options (A, B, C, D) aloud; so does every NOVA message in Teach NOVA and Ask NOVA. Tap again to stop. Helps younger students, weaker readers and students with reading difficulties.
- **🗣 Talk with NOVA.** In Teach NOVA, "Talk with NOVA" makes it a spoken conversation: NOVA's message is read aloud, then the microphone listens; what the student says is sent to NOVA, and NOVA's reply is read aloud in turn, until the student taps it off. (The page no longer reloads mid-conversation.)
- **In the student's language.** Reading aloud and listening use the student's language from Me (hi-IN, ta-IN…; English is en-IN), so a Hindi student hears Hindi and can speak Hindi to NOVA (with Claude, NOVA understands and replies in it). The 🎤 Speak buttons now listen in the student's language too.
- **Uses the phone's own speech, free.** Reading aloud uses the phone's or browser's built-in voices: nothing to install and no cost. NovaQuest never records or keeps audio; only the words the student said reach NovaQuest, as typed text. Note that the phone's speech-to-text may itself be a cloud service (in Chrome, Google's), and the parent notice now says so. Where a browser can't speak or listen, the buttons simply don't appear. Chrome on Android supports both; voices for Indian languages depend on the phone.
- No database changes.

## 1.22.0 (26 Oct 2026): Hindi and regional languages for students

- **My language.** On Me, a student chooses English, हिन्दी (Hindi), தமிழ் (Tamil), తెలుగు (Telugu), ಕನ್ನಡ (Kannada), മലയാളം (Malayalam), मराठी (Marathi), বাংলা (Bengali) or ગુજરાતી (Gujarati).
- **The app's words in Hindi and Tamil.** Menus, buttons and the main student screens (Home, Tasks, assignments, Daily 5, the learning-path steps, sign-out) are in Hindi or Tamil. In the other languages these words stay in English for now; NOVA still speaks the language. The page is marked with its language for screen readers.
- **NOVA speaks the student's language** (with Claude): Ask NOVA answers, assignment hints and Teach NOVA's replies are translated, keeping the English science word in brackets the first time ("कोशिका झिल्ली (cell membrane)"). Students can also ask NOVA in their own language: the question is put into English to search the textbook.
- **Read a question in my language.** With Claude, each assignment question has "Read in हिन्दी" (or their language); tap again for English. Number problems keep the student's own numbers.
- **English underneath.** Questions, answers, marking, NOVA's safety checks and everything teachers see stay in English; only what the student reads is translated. Each sentence is translated once and kept for the school, so it's fast and cheap the next time.
- Without Claude, the Hindi and Tamil screens still work; NOVA replies in English.
- Database: migration 015 (each student's language, kept translations), applied by `nq up`.

## 1.21.0 (26 Oct 2026): Marking work done on paper, from photos

- **Paper work.** On any scheduled assignment, "📷 Paper work" lists every student with where their work stands: handed in online, photos waiting to be checked, marked from paper, or nothing yet. For students who wrote on paper (the printed worksheet or an exercise book), photograph their answers and upload them against their name: up to 6 pages, photos (JPEG or PNG, straight from a phone camera) or a PDF scan.
- **Claude reads the handwriting** in the background (about a minute): each answer typed out exactly as the student wrote it, never corrected, with the letter they chose for multiple choice and the final number for number problems. Anything hard to read is marked **Unclear**.
- **You check before anything is marked.** "Check and mark" shows the photos beside what was read; fix any misreading, tick "Read correctly" for unclear answers you've checked, then **Mark this work**. Without Claude, you type the answers from the photos on the same page.
- It's then marked exactly like online work (multiple choice and number problems exactly, with the student's own numbers; written answers against their marking points), counts in results, mastery, growth and briefings, and goes through the usual review and release. Unclear answers you didn't confirm are flagged in "answers to check".
- **Data protection:** photos are kept in the NovaQuest data folder (never in the release) and deleted with the school's AI-conversation period, or when a student is erased; marks stay. A student's data export includes what was read from their paper work, and the parent notice now says photos of paper answers may be taken and read by Claude.
- Database: migration 014 (paper work), applied by `nq up`.

## 1.20.0 (26 Oct 2026): Number problems for maths and science

- **New assignment type: Number problems (maths, science).** Questions whose answer is a number, set in everyday situations: areas, speeds, pressure, density, Ohm's law, percentages, simple interest, mean, volume, work, acceleration, Pythagoras, simple equations, weight… With Claude, they're written from the topic's own method; without Claude, NovaQuest picks from its ready-made problems that fit the topic.
- **Each student gets their own numbers.** "A field is {l} m long and {b} m wide…": every student sees different numbers (the same each time they open it), so copying a neighbour's answer doesn't work. The answer, hints and worked solution are worked out for each student's numbers.
- **Marking that understands how students write numbers** — no AI, instant:
  - 24, 24 cm², 24cm^2, 24 sq cm, 1,200, 1,20,000, 3/4, 1 1/2, 2.5 × 10^3, −5, 45%, ₹ 250, "A = 24 cm²" all read correctly.
  - Units are converted: 0.0024 m² is right when the answer is 24 cm²; 120 N/m² is right for 120 Pa.
  - The right number with a missing or wrong unit gets half marks and "check the unit".
  - Close but outside the allowed range gets half marks and "check your rounding"; off by 10, 100 or 1000 says "a unit slip?"; the right size with the wrong sign says "check the sign".
- **Step-by-step hints.** "Need a hint?" on a number problem gives the method one step at a time with the student's own numbers ("Step 1 of 2: Pressure = force ÷ area."), never the answer, and without calling Claude.
- **Worked solutions.** After results are released, the scorecard shows the full working with the student's own numbers. Worksheets and the projector view show it as the answer key.
- **For students:** a number box that opens the number keypad on phones, and buttons for ² ³ √ π × ÷ − / that type where the cursor is.
- **For teachers:** the draft shows the answer, unit, allowed range, hints and worked solution. "Add your own question" has a Number problem type: the answer (or number ranges and a formula, so each student gets their own numbers), unit, how close an answer must be, hints and a worked solution. NovaQuest checks the formula works for every student's numbers and refuses hints that give the answer away.
- Database: migration 013 (the new question type), applied by `nq up`.

## 1.19.0 (26 Oct 2026): Data protection pack

For India's Digital Personal Data Protection Act 2023: children's data needs a parent's consent, and parents can see, correct or erase it. A new **Data protection** page for the school admin (left menu):

- **Consent register.** Every student, current and left, with who gave consent, how and when. Download as CSV for the school's files.
- **Withdraw consent.** The student is signed out at once and can't sign in until a teacher records consent again.
- **Export a student's data.** One file (JSON) with everything NovaQuest holds about that student: profile and consent, every answer with marks and notes, topic progress, learning activity, Ask NOVA and Teach NOVA conversations, escape rooms, Daily 5 and notifications. For a parent's request to see it.
- **Erase a student.** On a parent's request: the admin types the student's name to confirm. Name, parent email, PIN, conversations with NOVA, written answers and teacher notes are deleted, and the student can't sign in. Marks stay without a name, so class figures don't change. It can't be undone.
- **How long we keep data.** The school chooses: delete conversations with NOVA after 3, 6, 12 (default), 24 or 36 months, or keep them; erase students who have left after 3–36 months (default 24), or keep them. Checked once a day.
- **Parent notice and consent form (PDF).** One page in plain words: what NovaQuest keeps, who sees it, how AI is used (text sent to Claude, no names except a first name in teacher notes), how long data is kept with the school's own settings, and parents' rights, with a consent form to sign.
- **AI conversation log.** The principal (new "AI conversations" menu item) and the admin can read what students asked NOVA and taught NOVA, filtered by class, period (7 days to a year) and words (for example "sad"). Each look is recorded, and the admin's page lists recent data-protection actions.
- Teachers can't see any of this.
- Database: migration 012 (retention settings, erasure and withdrawn consent), applied by `nq up`.

## 1.18.0 (26 Oct 2026): Weekly class briefing for teachers

- **Briefing tab.** Each class has a new **Briefing** tab (Classes → the class → Briefing) covering the last seven days:
  - **Activity:** students active, work handed in, Daily 5 reviews done.
  - **Top mix-ups:** the wrong ideas most students showed in this week's answers, with how many students.
  - **Hardest topics:** topics the class averaged under 60% on this week (assignments, practice, checks and Daily 5).
  - **Students to check on,** each with plain reasons: "Didn't hand in Cell Lab", "Scores fell to 30% this week (usually 90%)", "No activity this week (active the weeks before)". Most reasons first.
  - **A 10-minute reteach activity** for the week's biggest mix-up (or hardest topic): a 2-minute hook (the wrong idea on the board, hands up), 4 minutes putting it right (from the approved lesson), a 3-minute check with an approved multiple-choice question and its answer, and a 1-minute exit ticket.
- **Every Monday from 7 am,** each teacher gets the briefing for each of their classes as a notification and an email, once a week. The principal can open any class's briefing too.
- "Download (PDF)" for a department meeting. Teacher Home links to each class's briefing.
- Built from data NovaQuest already has: no Claude calls, no running cost. No database changes.

## 1.17.0 (26 Oct 2026): Daily 5 spaced review

- **Daily 5.** A card on every student's Home: five quick multiple-choice questions a day on topics they have already met, about two minutes. Done once per day; the card then says "Done for today ✓" with their streak of days in a row.
- **Spaced repetition.** Each topic a student has practised or been assessed on gets a review box. Right answer: the topic comes back later (1, 2, 4, 7, 14, then 30 days). Wrong: it comes back tomorrow. Topics that are due come first, weakest first; if fewer than five are due, the oldest-seen topics fill in.
- Questions come from the teacher-approved question bank and approved lesson checks first, then from the textbook, never the same question twice in one day. Marked instantly, no AI calls.
- Every answer feeds the student's topic mastery, so "Topics to improve" and teachers' views reflect review too.
- Includes 1.16.0 (start-of-term checks and learning growth), which wasn't released on its own.
- Database: migration 011 (review boxes and daily reviews), applied by `nq up`.

## 1.16.0 (26 Oct 2026): Start-of-term checks and learning growth

- **Start-of-term check.** On a class's new **Growth** tab (Classes → the class → Growth), a teacher sets the start-of-term check for a subject with one button: about 20 multiple-choice questions, one or two from every chapter of the book. It arrives as a draft assignment: check the questions, then schedule it like any other.
- **Progress checks.** Later in the term ("Set a progress check"), NovaQuest drafts a check on the same topics with fresh questions, so the two can be compared. Mid-term and end of term work well; there can be as many as you like.
- **The growth report.** The Growth tab shows the class average at the start of term and now, the change in points, how many students improved, each student's start, latest and change, and each chapter's class average then and now. Only students who took both checks count towards the class's change. "Download (PDF)" gives the same report for a meeting.
- **For the principal:** each class's page has a "Learning growth" line per subject ("42% at the start of term → 61% in Progress check 1 (+19 points)") and can open the full Growth tab.
- Multiple choice only, so every check is marked the same way, instantly, with no AI marking.
- Database: migration 010 (which assignments are checks), applied by `nq up`.

## 1.15.0 (26 Oct 2026): Students see their work across subjects

- **Subject tags everywhere.** Every assignment on a student's Home and Tasks carries a small coloured tag for its subject ("Science", "Maths"; "Science (IGCSE)" when two books share a subject name). Each subject keeps its colour.
- **Home, with several subjects:**
  - **Up next** is still one card: the assignment closing soonest, in any subject, now with its subject tag and, if started, how far the student has got.
  - **A line per subject** ("Science · 3 to do · 1 started", "Maths · 2 to do"); tapping one opens Tasks for that subject.
  - **In progress:** work they've started but not handed in, with "2/5 answered" and time left.
  - **To do:** work not started, nearest deadline first; anything closing within a day is highlighted.
  - At most four besides Up next, then "See all N tasks". Results and learning suggestions stay below, as before.
- **Tasks:** filter buttons (All · Science · Maths), and "To do" is split into **In progress** and **Not started**; every row shows the subject, progress and deadline.
- Everything is sorted by deadline across subjects, so the most urgent work comes first whatever the subject.

## 1.14.3 (26 Oct 2026): Refresh a book's text

- **Refresh the book's text.** A new "Book text" panel on each book's page (Books → the book) reads the stored PDF again with NovaQuest's latest reading (for example, 1.14.2's clean-up of diagram labels) and replaces the text under each existing topic. Topic names and ids, outcomes, lessons, questions, escape rooms and students' progress are all kept. It runs in the background (a minute or two; a scanned book reuses its saved page text) and the page says how many chapters were refreshed.
- A chapter whose topics can't be found in the new text (for example a topic renamed so it no longer matches the book's heading, or topics removed during review) is left exactly as it was and listed. If the PDF isn't on the computer, it says so and what to do.

## 1.14.2 (26 Oct 2026): Diagram labels out of the text; clearer practice questions

- **Diagram labels no longer land in sentences.** A PDF gives the labels beside a figure ("Electric lamp", "Electrode 1", "Electrolyte (Liquid)") in reading order between the lines of the paragraph next to it, so the book's text read "A chemical reaction between the Electrode 1 rods and the electrolyte…", and NovaQuest's basic questions copied that. Reading a book now drops a label that sits inside a sentence, a label glued onto the end of a line, stray label words on their own line ("container", "lamp"), and a figure caption printed between two halves of a sentence. Headings and two-line titles are kept. Books uploaded from now on get the clean text.
- **Topics titled as questions:** the basic writer asks "How Does a Battery Generate Electricity? Which statement is correct?" instead of "Which statement about how does a battery generate electricity is correct?".
- **Practice:** when the school's question bank has too few questions on a topic, practice now uses the topic's approved quick checks and picture-quiz questions (drafted by Claude and checked by the admin) before any made by the basic writer.
- A chapter title on its first page no longer picks up lower-case text from a picture above it ("of oil and sugar in diet").

## 1.14.1 (26 Oct 2026): Uploaded books survive upgrades

- **Books kept outside the version folder.** Uploaded books (their PDFs and the page text read from scans) were kept in the version folder's `var\books`, so each upgrade, unzipped into a new folder, started without them: Book pages couldn't show and Read again had to start over. On Windows they now live in `%LOCALAPPDATA%\NovaQuest\data`, next to the database, and stay put across upgrades (`NQ_DATA_DIR` still overrides).
- **Brought across automatically:** `nq up` copies every uploaded book that isn't there yet out of earlier NovaQuest folders next to the new one (for example `Downloads\novaquest-1.13.1\novaquest-1.13.1\var\books`), newest copy first, never overwriting; the old folders are left as they are.
- **Told when a file is missing:** `nq status` has a "books" line, and a book's page marks a file "Not on this computer" with what to do.
- The folder `data\source` is no longer part of NovaQuest (since 1.12.0, when the old NCERT book was retired).

## 1.14.0 (26 Oct 2026): Needs to improve, per student and subject

- **Needs to improve.** Each student's page (Classes → class → student) now opens with the topics that student most needs to work on, for each subject (book) the class studies, worst first. Each topic says why, in plain words:
  - a weak score on that topic's questions in an assignment ("Scored 40% in Cell Lab");
  - a weak last practice, quick check, understanding check or picture quiz ("Last practice: 40%");
  - a mix-up seen in their answers ("Mixed up: cell wall and cell membrane (2×)");
  - a topic the class has had work on that the student hasn't started.
  Mastered topics and strong results are left out; the full topic list stays below.
- **One-step revision task:** "Set … a revision task on the top 3" opens a new assignment for just that student, with those topics ticked (the teacher picks the type and checks the questions as usual).
- **Parent report:** "Worth revising together" now lists the same topics, each with its reason.

## 1.13.1 (23 Oct 2026): Practice from the school's own questions

- **Better practice questions.** A student's practice mission in Learn now uses the school's approved multiple-choice questions on that topic first: ones Claude or a teacher wrote and the teacher approved, from assignments whose results have been released. Before, practice always used NovaQuest's basic writer, which tests the book's wording rather than understanding.
- **No giving away live work:** a question in any assignment still waiting for its results never appears in practice; "Basic" questions never do either.
- **A new mix each round:** practising a topic again brings the next questions from the bank. When the bank has fewer than five for a topic, questions made from the textbook fill the gap (as before).
- Practice is still marked instantly and makes no Claude call.

## 1.13.0 (23 Oct 2026): Messages to students, parent reports, and Sign out at the top

- **Sign out at the top right.** Every staff page (teacher, school admin, principal) has an account button at the top right with your name; it opens Sign out, and Switch role for people with two roles. It used to sit at the bottom of the side menu. Phones keep the same menu in their top bar.
- **A message to the whole class with the results.** "Release results" has an optional "Message to the class" box ("Well done, everyone! Revise 8.3 before Friday."). Every student who handed in sees it at the top of their results, and the results notification says there's a message. It can be updated after release (students are notified again).
- **Notes to one student reach them.** A note written after the results are out now reaches the student's bell and Home ("Mrs Rao left you a note"); before release it arrives with the results, and the results notification says so. Students see "to the class" and "to you" notes separately; nobody sees another student's note.
- **Quicker notes:** one-tap starters ("Great effort!", "Please revise this topic: …") for both boxes, and **Suggest a note from the answers** on a student's page: Claude (or NovaQuest's own wording without Claude) drafts 2-3 honest sentences from that student's results, naming a strength and the one topic to revise. It only fills the box; nothing is sent until the teacher saves.
- **Progress reports emailed to parents.** On a class's Students page, **Email reports to parents** sends each parent a private two-week link to their child's progress report (no account needed), only for students with recorded consent and a parent email; the page says how many it can reach and when they were last sent. The report (web and PDF) now includes the teachers' notes.
- **Claude use on School setup.** The school admin sees today's Claude use against the school's daily limit, the last 30 days' requests and estimated cost, and how often the offline engine stood in.
- **Teachers' own questions.** "+ Add your own question" on a draft assignment: multiple choice or short answer (model answer and marking points), on one of the assignment's topics. It's approved as written, joins the question bank, and is marked like any other.
- Database: migration 009 (the class message; when parents were last emailed), applied by `nq up`.

## 1.12.0 (23 Oct 2026): Escape rooms for every book; the old NCERT book retired

- **Escape rooms for your own books.** Escape rooms existed only for the built-in NCERT 2018 book, so an IGCSE class had none. Now, once a school's book is published, the admin presses **Draft escape rooms with Claude** on its page: Claude writes one room per chapter from the chapter's own text (a story and four locks: sort NOVA's examples by clues, fix its mislabelled training data, check a confident claim against evidence, and read a data table honestly). NovaQuest checks every lock the way it checks the built-in rooms (a sorting lock must be solvable, and the clues chosen must matter); a draft that fails goes back to Claude once with the reason, and a broken lock is replaced by Claude's spare.
- **The admin approves each room.** Each drafted room is listed as "To check" with a preview showing the answers and door code; **Approve** (or Approve all) makes it live, **Redraft** throws it away for a new one (not once students have played it). Students then find the room on Learn (under its chapter) and on Escape rooms, grouped by book; teachers see it on the class's Escape rooms page and its answer guide. Each book's rooms have their own door codes. Opening a lock counts as learning on that book's topic.
- **The NCERT Class 8 Science book (2018) is retired.** It is out of date, so it is no longer offered when the admin picks a class's books or maps a teacher, is taken off every class, and a new class starts with no book. Past assignments, answers and progress on it are kept and still open. `nq init` no longer loads it or asks for its PDF; upload your books (including NCERT's current "Curiosity") under Books.
- **Demo school:** `nq seed` now uploads a small original two-chapter demo book as the demo school's own book (read and published like any book), and sets its demo mission from it.
- Database: migration 008 (escape rooms per book; escape runs per book; the old book archived), applied by `nq up`.

## 1.11.2 (22 Oct 2026): Teach NOVA follows the conversation

- **NOVA answers what the student said.** Claude was given only NOVA's wrong idea, the verdict and a fixed follow-up question, never the student's words, so NOVA asked the same question ("Which part of the root makes the surface so big?") after it had been answered. NOVA now gets the student's message, the last few messages, the ideas already taught and the ones still missing, and asks about a missing one in its own words.
- **Ideas taught over several messages add up.** A misconception used to count as fixed only when one message covered every key idea. Now each correct message's ideas are added together, and NOVA's idea is fixed once all have been taught. A message that says something wrong adds nothing, so NOVA still never learns a wrong idea.
- **"Yes, but not…" isn't agreement.** A message starting with "yes" was marked as agreeing with NOVA's wrong idea ("NOVA is confused. Check your idea!") even when it went on to correct it. Now only real agreement counts ("Yes, you're right…", a bare "yes").
- **No repeats, no backsliding.** A NOVA reply that asks the same question as its last message, or brings back its old belief ("so plants do get *some* water through their leaves…"), is replaced. After three tries on the same idea, NOVA says plainly which part it still doesn't get.
- The evaluator also sees the recent chat, so a short answer to NOVA's question ("root hairs") is judged as an answer.

## 1.11.1 (22 Oct 2026): Scanned books on Windows

- **Tesseract had no English:** the official Windows Tesseract installer, run silently by the prerequisites setup, installs no language at all, so Tesseract stopped on every page ("returned non-zero exit status 1") and the book said "upload a clearer copy". The setup (installer 1.0.4) now also puts Tesseract's English file in NovaQuest's own folder (`%LOCALAPPDATA%\NovaQuest\tessdata`, set as `NQ_TESSDATA` in `.env`): the same file NovaQuest is tested with (tessdata_fast 4.1.0), checked against its sha256. NovaQuest passes that folder to Tesseract, so it works whatever languages Tesseract itself has, with no admin rights needed.
- **Plain reasons:** if Tesseract can't read English, the book says so ("Tesseract has no English language file") and what to run, and `nq status` does too. If every page still fails, the book shows Tesseract's own message instead of "upload a clearer copy". Failed pages are never saved, so Read again tries them afresh.
- The log now records why a page failed (Tesseract's or pdftoppm's message), not only the exit code.

## 1.11.0 (22 Oct 2026): Scanned books can be read

- **Scanned books:** a book whose pages are pictures (a scan, with no selectable text) is now read with text recognition (Tesseract OCR). Before, it failed with "almost no text NovaQuest can read". The book page shows "page n of N" while the pages are read (about 2–4 seconds a page, a few at a time), then the book carries on as usual. The review note says the pages were scanned, so the admin checks the chapter and topic titles closely.
- **Each page is read once:** every page's text is saved as soon as it is read, so a restarted worker, or reading the book again, carries on from where it stopped. A text PDF is never sent to OCR, and a few blank pages in a text PDF don't count as a scan.
- **Read again:** a book that couldn't be read has a **Read again** button. If Tesseract isn't installed, the book says so and what to run; after installing, press Read again.
- **Books numbered like Cambridge's:** a whole book in one PDF without "Chapter N" headings is split at its numbered sections ("1.1 Photosynthesis", "2.1 Atomic structure…"): each unit's title comes from its running header. The contents page doesn't start a chapter; numbered end-of-unit questions ("2.5 The diagram shows…") aren't topics; "Check your progress" ends a unit's topics.
- **Clean titles from scans:** the arrow marks before a heading ("» 1.1") and the "In this topic you will" box that recognition glues onto a heading are left off; a heading cut short on its opening page takes the version printed most often.
- **Installer 1.0.3:** NovaQuest-Prerequisites-Setup.exe now also installs Tesseract OCR 5.5.3 (the official Windows build, verified against GitHub's sha256). `nq status` says whether scanned books can be read. The Docker image includes Tesseract.

## 1.10.11 (22 Oct 2026): Questions that test understanding

- **Claude is asked for understanding, not wording.** Its instructions now say: write in your own words; make the student use an idea (explain why, apply it to a new everyday situation, compare, predict, spot the mistake); every question stands on its own (no tables, figures, activities or page numbers); wrong options are believable mistakes, never one sentence with a word swapped; no "Which statement about … is correct?" built from book sentences.
- **Claude's questions are checked for that:** questions whose options are one sentence with a word swapped, or that point at a table, figure or activity, are rejected. A passage id written as text still counts as a citation.
- **Replies no longer cut off:** a ten-question mission ran past the fixed 4,000-token reply limit, so Claude's reply was cut off mid-question ("bad JSON: Unterminated string") and every question went to the basic writer. The reply now gets room for the questions asked; if one is still cut off, the questions that arrived whole are kept and Claude is asked only for the rest.
- **No silent fallback:** if Claude's reply can't be used, NovaQuest asks once more. Questions it still can't get from Claude are written by the basic (offline) writer and marked **Basic** on the teacher's draft, with the reason when the draft is made, and a **Rewrite basic questions with Claude** button that puts Claude's questions in their places. Before, a failure quietly replaced the whole set.
- **Basic writer:** skips sentences that mention tables, figures or activities (plural too: "In Tables 2.1 and 2.2…"), and sentences with a figure label glued to the front ("Nucleoid Some microorganisms…").
- Database: questions record who wrote them (migration 007, applied by `nq up`).

## 1.10.10 (22 Oct 2026): Preview Back/Next under the card

- On the staff previews of a picture lesson (a teacher's preview, and the school admin's check of a drafted topic) the Back/Next bar now sits under the card. It floated over the card's content, because it was placed to clear the students' phone menu bar, which staff pages don't have. Students keep it fixed above their menu bar.
- The teacher preview's note now says to use Back and Next (it wrongly said every card was on one page).

## 1.10.9 (22 Oct 2026): Approvals that add up

- **No phantom "to approve":** the Books list counted a topic whose picture lesson couldn't be drawn as an item to approve, so it showed "1 to approve" with nothing to approve. It now counts exactly what the book's page counts: picture lessons, quick checks and mix-ups waiting for you.
- **Pictures can be retried:** a topic without a picture lesson ("No pictures") is now treated as missing, so "Draft any missing topics" tries its pictures again (once per run). Quick checks and mix-ups you already approved stay approved. The panel says how many topics have no picture lesson.
- **Quick checks labelled:** each topic shows "Quick checks to check" or "Quick checks ✓", like pictures and mix-ups.

## 1.10.8 (22 Oct 2026): Lesson drafts no longer fail in pairs

- **Temperature refusal with drafts in parallel:** Sonnet 5.5 refuses the temperature setting. When three drafts went out together, only the first refusal back resent without it; the other two counted as failed ("couldn't be drafted"), and every new batch repeated this. Now every call that sent the setting resends without it, the refusal is remembered for the rest of the run, and the resend doesn't use up one of the call's retries.
- **Text after the JSON:** a reply with a note or a second object after the draft is read from its first complete object, instead of failing as "bad JSON: Extra data".

## 1.10.7 (21 Oct 2026): Lesson drafting that finishes

- **Drafting no longer times out on every topic.** A topic's picture lesson, quick checks and mix-ups is a long piece of writing (up to 6,000 tokens, about a minute), but every Claude call had the 20-second limit meant for students' hints, so each draft timed out and was retried twice. Long replies now get time for their length; hints and answers keep the short limit.
- **Three topics are drafted at once**, so a 63-topic book takes roughly a third of the time.
- **When Claude is busy** (rate limit), background drafting waits as long as Claude asks instead of giving up; a reply that times out twice stops after two tries.
- **Honest progress:** a topic that couldn't be drafted shows as "couldn't be drafted", not as done, and isn't retried in the same run. The run then pauses, and Continue drafting tries those topics again.
- **Restarts:** jobs interrupted by a restart or upgrade are picked up again when NovaQuest starts, and the book page offers Continue drafting whenever no drafting is actually running.

## 1.10.6 (21 Oct 2026): Uploaded books read as printed

Tested on the real NCERT 'Curiosity' Grade 8 Science book (13 chapters and its front matter):
- **Chapter titles** come from the book's own contents page when the front-matter file is uploaded (e.g. hecu1ps.pdf); otherwise from the title printed beside the large chapter number. Text in pictures above a title (chapter 3's bulletin board) is no longer taken as the title.
- **Topics are the book's own numbered sections** (3.1, 3.2 …) when it numbers them, with headings that wrap onto a second line kept whole. Sub-sections (3.2.1, 3.2.2) stay inside their section. Claude's outline can no longer split or renumber them; its learning outcomes are kept where its topics match.
- **No repeated numbers:** a printed section number is taken off topic titles, so a topic no longer shows as "3.1 3.1 Health: …".
- **Whole chapters:** a sentence that merely begins with words like "Index", "exercise" or "what you have learnt" no longer ends a chapter's topics early, and "Curiosity's" own end sections (Snapshots, Keep the curiosity alive, Discover, design, and debate) are recognised. Hidden control characters after section numbers no longer hide sections.

## 1.10.5 (21 Oct 2026): Uploaded books: whole titles, right chapter numbers

- A chapter title printed over several lines is read whole ('The Invisible Living World: Beyond Our Naked Eye', not 'The Invisible Living'). With a Claude key, Claude's fuller title also completes one that was cut short.
- Chapters without a 'Chapter N' heading keep their own number, read from the large number on the first page (or NCERT's QR label, such as 0877CH02).
- Front-matter files (cover, foreword, contents, e.g. hecu1ps.pdf) are no longer read as a chapter. They had pushed every chapter's number up by one.
- `nq init` now names only the chapters whose passages it actually removed.

## 1.10.4 (21 Oct 2026): Textbook files from another book

- A chapter PDF now counts as one of the 2018-19 book's chapters only if its pages carry that chapter's printed page numbers, not just its title. Before, NCERT's newer 'Curiosity' book (files hecu101 to hecu113) could slip in: its 'Light: Mirrors and Lenses' was taken as chapter 16, 'Light', so Ask NOVA quoted the other book with the wrong page numbers and chapter 16's Book pages showed the wrong pages.
- `nq init` removes passages an earlier version took from such a file and says so, and explains that hecu files are the newer 'Curiosity' book (the built-in syllabus is the 2018-19 book, files hesc101 to hesc118).

## 1.10.3 (21 Oct 2026): Faster Claude calls

- Claude calls no longer wait 20 seconds before starting. On some networks (often IPv6 on Windows) the first address the API resolves to never answers, and each call waited the full 20-second timeout before trying the next. NovaQuest now tries IPv4 first and gives each address at most 3 seconds to connect. A hint that took about 21 seconds should now take 1 to 2.
- `tools.smoke_claude` shows how long each address (IPv4 and IPv6) takes to connect, and how long each model took to reply.

## 1.10.2 (21 Oct 2026): Clear Claude connection errors

- When Claude refuses a call, NovaQuest now reports Claude's own reason (for example "Your credit balance is too low") instead of only "model error 400", in the log and in `python -m tools.smoke_claude`. The key is never shown.
- `tools.smoke_claude` first checks each model with one tiny call and, if one fails, says what to do (add API credit, replace the key, fix the model name in .env, or wait out a rate limit), instead of five failed checks.
- If a model replies that it doesn't accept the temperature setting, the call is resent once without it, and that model is called without it from then on.

## 1.10.1 (21 Oct 2026): Display fixes

- **Projector view:** after "Show answer", the answer and "..% of the class got this right" no longer sit under the bottom control bar. The answer line for a multiple-choice question is now just the letter and the class's result (the right option is already highlighted), and the slide tightens up when revealed. Checked on a laptop (1366 x 900) and a typical projector (1280 x 720).
- **Exam papers:** each set's marks by chapter are listed in book order (chapter 8 before chapter 11; they were sorted as text).
- **Results tiles:** a tile with no figure says why. Before anyone hands in: "Shown once students hand in". For a task with no AI questions, the AI skills tile says "This task has no AI questions" instead of a bare dash.

## 1.10.0 (20 Oct 2026): Exam papers

For a teacher setting a class test or exam (**Assignments → Exam papers**, or **Exam paper** on Home):
- **Your pattern.** Pick a common one (25 × 1; 10 × 1 + 5 × 3; 20 × 1 + 5 × 2 + 4 × 5; 10 × 1 + 10 × 2 + 5 × 5; an 80-mark board pattern) or make your own: up to 6 sections, each "how many questions × how many marks" (½ to 10), with a running total. The kind of question follows the marks (1 mark multiple choice, 2–4 short answer, 5+ a long answer in two parts) unless you choose.
- **Your difficulty.** Easy, medium, hard, or mixed (30% easy, 50% medium, 20% hard in every section).
- **Your topics.** Tick topics or whole chapters; the chapter in class now is ticked to start.
- **Three options.** NovaQuest writes Sets A, B and C to the same pattern. Each shows its marks by chapter, difficulty and thinking level, and how many questions it shares with the others (said plainly when the topics can't give enough different questions). No set repeats a question, or the same sentence with a different word blanked.
- **Swap and choose.** Replace any question with a new one of the same kind and marks that isn't anywhere in the paper. Choosing a set saves its questions to your question bank.
- **Print-ready PDFs** on A4: school, title, class and subject, set, time allowed and maximum marks; name, roll number and date lines; general instructions plus your own; "SECTION A — 10 × 1 = 10 marks" headings; marks against every question; page x of y. Optional ruled lines to write answers on the paper. A separate **answer key and marking scheme** gives every multiple-choice answer and each written answer's points with their marks.
- Also fixed: the topic count no longer counts the "All topics in this chapter" box.

## 1.9.0 (20 Oct 2026): For the principal

From a principal's review of their own screens:
- **Needs your attention** at the top of the Overview, worked out from the data, each item linking to where to act:
  - classes with no activity for a week or more, and their teacher;
  - classes behind their term plan;
  - classes handing in under 70% on time, or with low understanding;
  - teachers who haven't set work for two weeks, with a **Send a friendly reminder** button that puts a note in the teacher's bell (at most one a day);
  - students who've missed 3 or more assignments. Only those are named now; one or two missed stays on the class teacher's list.
- **Every row leads somewhere.** Each class opens a new read-only class page:
  - its figures and term plan (on track or how many chapters behind, and what's in class now);
  - weakest topics, and the work set with how many handed in;
  - AI skills from the escape rooms;
  - every student with their topic levels, missed work and rooms escaped.

  From there the principal can open the progress grid, escape rooms, results, students, tutor logs, Teach NOVA transcripts and parent reports, all read-only. Links that used to end on "You don't have access" (the Students and Term plan tabs, back links) now go to the right place. A test follows every link a principal can reach and fails on any error page.
- **Honest AI skills.** A skill shows only with 5+ students behind it (it showed "100%" from 6 children before), and the classes table shows rooms escaped and played per class.
- **The classes table** adds term-plan status, escape rooms and last activity (classes with no activity are marked); it turns into cards on a phone. A "What do these numbers mean?" panel explains each figure.
- **Weekly summary**: every Monday from 7 am, each principal gets the week's active share and "needs your attention" list in their bell and by email.
- **Monthly report (PDF)**: one page for management meetings, for any month:
  - students, activity, work set, escape rooms and on-time share;
  - each class's month, with term-plan status;
  - teachers, AI skills, and what needs attention.
- **Syllabus** is a read-only view for the principal (no "lock a topic" instructions); teacher-only buttons are hidden on assignment pages.

## 1.8.0 (19 Oct 2026): Escape rooms, and one way to show progress

- **Escape rooms: learn AI through AI.** Every chapter of NCERT Class 8 Science has a room (18 in all). The student and NOVA, the class AI, are locked in; NOVA keeps making mistakes, and each of the four locks is an activity that fixes one:
  - **Train NOVA's sorter:** choose the clues NOVA looks at, then watch it sort new cases. A clue that only happened to fit the examples ("looks green") fools it on an onion cell.
  - **Fix NOVA's training data:** find the examples labelled wrongly (yeast filed under bacteria).
  - **Check NOVA's claim:** NOVA is "95% sure"; decide whether it's right and pick the fact that settles it.
  - **Pattern or cause:** read a small data table and choose the honest conclusion or prediction.

  Every lock joins the chapter's science with one of six AI skills, and gives a digit of the door code. A wrong try is explained without giving the answer away; a hint costs a little. Rooms are on Learn (and at the end of each chapter), work without a Claude key, and are checked when loaded: every sorting lock can be solved, and in each one a misleading clue fools NOVA.
- **AI skills are measured** from the locks (first-try solves score highest). Students see each skill as Not yet / Learning / Strong. Teachers get an Escape rooms tab per class (each skill across the class, and who has escaped which room) and an answer guide for every room, with the door code. The principal's AI skills figure now comes from the rooms.
- **One measure of progress for students: topic levels** (Learning, Practised, Mastered).
  - Scorecards now say how many were right and show each topic's level ("Your topics now"); the Science and AI skills percentages and "big ideas" are gone.
  - Me lists results as "3 of 3 right", and topic pages say which step reaches the next level ("Next: Practise → Practised").
  - The parent page and report PDF match. Teachers still see the detailed percentages.

## 1.7.1 (19 Oct 2026): Fixes from the UX review

- **Teachers on a phone:** the menu is now a bar at the bottom (like the students' app) instead of a strip that cut off "Assignments"; a slim top bar has the bell and an account button with Sign out (which phones couldn't reach before). The class list and term plan show as cards, not squeezed tables.
- **Honest numbers.**
  - The class card measures the whole syllabus for the whole class ("1 of 6 students started · <1% practised"), not just the topics someone has touched (it used to say "Practised 100%" after one student practised one topic).
  - The principal's figures need at least 5 students with scores in a class, chapter or week; below that they say "Not enough data yet".
  - "On time" counts only assignments whose deadline has passed, over every student they were set for, so an open assignment no longer shows as "0% on time".
  - The empty AI skills tile is hidden, and "1 assignments" now reads "1 assignment".
- **Better questions without Claude:**
  - Blanks no longer give the answer away ("_____ (pseudo: false; podia: feet)").
  - No question leans on an earlier class or the sentence before ("as you learnt in Class VII", "As a result, …"), and characters' lines from the book don't become questions.
  - Everyday words like "shape" or "size" are never swapped for science words (no more "organs vary in cytoplasm, size and number").
  - Wrong options are the same kind of word, "a"/"an" agree ("an embryo"), and titles that are questions read cleanly.
  - Vague wrong reasons ("To get sick.") are dropped.
- **Ask NOVA** on a topic page is a row in the list instead of a floating button over the cards; on other pages the end of the page is never hidden behind it.
- **New academic year** suggests the next grade's class of the same section (and says to add next year's classes first if there are none). Moving a class into the same grade in the same year is refused.
- **Parent link** opens a page a parent can read on their phone (what's done, chapters, results, what to revise), with "Download as PDF" on it, instead of downloading a file straight away.
- **Create assignment** starts with the chapter in class now (from the term plan) ticked and labelled.

## 1.7.0 (19 Oct 2026): Classroom tools

The last three items from the Grade 8 Science teacher's review:
- **Results question by question.** A new "By question" tab on an assignment's results shows, for each question, the share of the class that got it right, how many chose each option, the most common wrong answer, who didn't get it (with what they said) and what they mixed up. "Worth teaching again" lists up to three questions fewer than half the class got right, with one button to make a revision task on those topics.
- **Printable worksheet.** "Print worksheet" on any assignment gives a clean A4 page: school, class, name, roll number and date lines, lettered options, ruled lines for written answers, and (if you want it) the answer key on its own page. Only approved questions are printed, and it says how many are still waiting.
- **Projector view.** "Show on projector" shows one question per screen in large type. Use the arrow keys or buttons to move, and **A** (or the button) to show the answer and mark the right option. Once students have handed in, each answer screen says how many got it right. There's also a full-screen button.
- **Term plan** (a new tab under Classes, also linked from Home; the menu keeps its four items). Give each chapter its dates for each class and subject, or let NovaQuest spread the chapters over the term (Monday to Saturday, longer chapters get more days) and adjust them. It shows the chapter in class now, and which finished chapters the class is behind on (under half practised), with buttons to see who needs help or set a revision task. Home shows each class's current chapter and whether it's on track; the class progress grid opens on the chapter in class now; students see "In class now" at the top of Learn with the next topic to do.
- **Upgrading is just unzip and `nq up`:** `nq up` now applies database updates itself (this version adds one table for the term plan).

## 1.6.0 (19 Oct 2026): Better for students

From a Grade 8 student's walk through the app:
- **Honest results.** A low score is no longer greeted with praise: the scorecard says "This one was tricky: 0 of 3 right. That's OK. Let's go through the topic again together." A good score still gets "Excellent!". Each idea shows its percentage, and "What to do next" has a **Revise** button for each weak topic. Scores show as "1" not "1.0".
- **Ask NOVA out of the way.** In picture lessons and practice, a small 💬 button sits next to the progress bar instead of the big floating button covering the answers. The understanding check has no Ask button, because it's the student's own words.
- **Help to start writing.** The understanding check suggests how to begin ("It happens because…", "In this case…"), says two or three sentences is enough, and links back to the topic. Teach NOVA's starter buttons are short enough to read in full and wrap on small phones.
- **Practice says plainly** that the teacher doesn't see practice marks, so students can try freely.
- **Me page shows what's done first:** picture lessons, topics practised, understanding checks and topics taught to NOVA, with a **weekly streak** (weeks, not days, so a weekend off never breaks it). Each chapter's bar shows mastered, practised and learning, and every revision-plan topic has a Revise button.
- **Up next on Home** says how many questions, about how long (or the time limit), and which topics.
- **Easier sign-in:** the class can be typed as "8 b", "8-B" or "8B", and the page says what to do about a forgotten PIN (ask the teacher, who can make a new one in a minute).
- **Celebrations** when a task is handed in and when every step of a topic is done.

## 1.5.0 (19 Oct 2026): See each student clearly

- **One page per student** (click a name on the class list or the grid): level on every topic they've worked on with the last activity and score, assignments handed in, scored or missed, what they mixed up, their recent questions to NOVA, Teach NOVA sessions passed, and last sign-in. "Print for a parent meeting" prints a clean one-page view; the parent report PDF and tutor log are one click away. Only the student's teachers and school leaders can open it.
- **Who needs help with what** on Progress: students down the side, a chapter's topics across the top, each square the student's level (Not started, Learning, Practised, Mastered), plus how many topics each student is behind on. It opens on the chapter with the latest activity; pick any chapter or subject.
- **Revision for just the students who need it**: tick students on the grid and press "Make a revision task". Create assignment opens with the topics those students most need already ticked, and the draft's schedule form has just those students chosen.

## 1.4.2 (19 Oct 2026): Better questions without Claude

Measured over all 125 topics of the NCERT book, asking each topic for multiple-choice questions at each difficulty:
- **Medium** questions are now 85% beyond recall (was 2%): "What is the reason? …", "What happens if …?", "Which statement about … is correct?" (one true textbook sentence against three with one key word swapped).
- **Hard** questions are now 66% analysis (was 7%): "Which statement … is NOT correct?" and correcting NOVA's common mix-ups. Easy questions stay as recall (fill in the blank).
- **Believable wrong options, one right answer**: the same kind of word as the answer (singular/plural, ending, length, noun/adjective/verb), never a near-copy of it ("weeds"/"weeding"), never a name for an ordinary word, a unit or a general word ("compounds") that could still be true. Wrong reasons come only from other topics, so a "What is the reason?" question never has two right answers.
- Lab steps ("Draw lines…"), story lines and sentences that lean on the one before ("Hence, …") aren't used as facts. When this leaves too few good questions, the draft says how many it made.
- **No glued headings**: sentences where the PDF joined a heading onto the text ("Preservation by Sugar Jams…", "8.1 Discovery of the Cell Robert Hooke…") or broke a sentence are no longer used for questions or model answers. Story lines and instructions aren't used as facts.
- **Missions** have a different story line at every stage.
- **Model answers** are clean textbook sentences, one per marking point; topics titled as questions no longer produce "…??".
- **Regenerate** now replaces a question even when the new one shares its wording but tests a different fact.

## 1.4.1 (19 Oct 2026): Fixes from a teacher's review

- **Fair marking without Claude**: a written answer that only lists key words ("shape amoeba size") now earns at most a third and goes to the teacher's "To check" list, instead of full marks. Marking points are the textbook's ideas, credited by any of their key words, so explanations in a student's own words get fair credit.
- **Stricter "Mastered"**: one test, however good, makes a topic "Practised". Mastered needs the understanding check or Teach NOVA, or a strong test backed by other evidence. Each activity counts by its latest score, so a later poor result brings the level down.
- **The number of questions you ask for**: written-answer and understanding-check drafts now give as many different questions as asked (each centred on another key idea). If NovaQuest still can't write enough, the draft says how many it made.
- **Understanding checks stay on topic**: the "apply" part always comes from the same topic.
- **Missing work**: the review screen lists every student who hasn't handed in, and "Remind them" sends each one a notice while the assignment is open.

## 1.4.0 (19 Oct 2026): Book pages

- **Book pages** on every topic: students open the topic's real textbook pages, shown as images of the book's own PDF, so figures, tables and layout appear exactly as printed. Pages are rendered once and cached; only the topic's own pages can be opened, and a locked topic's pages stay closed.
- Shown for the NCERT book (whole PDF or chapter PDFs). For a school's own book they stay off until the school admin turns them on and confirms the school has the right to show the full pages (recorded in the audit log). Teachers and admins can open the same pages from the syllabus board and the Books pages.

## 1.3.0 (19 Oct 2026): Your school's own books

- **Books** (school admin): upload a textbook for any board, subject and grade, as one PDF or one PDF per chapter. NovaQuest reads it in the background, finds the chapters and topics (Claude when available, else the book's own headings) and cuts the text into cited passages. The admin renames or removes topics, then publishes.
- **Lessons for any book**: Claude drafts each topic's picture lesson, quick checks and NOVA's mix-ups from the book's text. Every draft is validated and waits for the admin's approval; drafting runs in small batches and continues where it stopped.
- **Several subjects per class**: School setup → Subjects gives each class one or more books; teachers are mapped per class and subject. Students get one tab per subject on Learn and Learn in pictures; Ask NOVA searches the class's books and names the right course; teachers pick "class · subject" when setting work; dashboards cover all of a class's books.
- **Private by design**: a school's books, topics, passages and lessons are visible only to that school (database row-level security). The shared NCERT book stays available to every school.
- AI prompts now name the course (grade, subject, board) instead of assuming Grade 8 Science.
- Update with `nq down`, unzip over the folder, then `nq init` (adds the new tables) and `nq up`.

## 1.2.2 (19 Oct 2026): Ask NOVA fixes

- Ask NOVA understands casual questions: "whats a bacteria", "tell me about plastics", "explain fermentation" and "wat is a cell" are answered from the textbook instead of being declined as off-syllabus. Off-syllabus questions asked the same way are still declined.
- Asked from a topic page, NOVA looks in that chapter first, then the whole book.
- Better offline answers: "what is…" questions prefer the sentence that defines the thing, and plurals match properly (virus/viruses).
- The textbook can now be one PDF per chapter, as downloaded from the NCERT website (any file names, any order). Each chapter is recognised by its title, and `nq init` reports which chapters it found and which are missing.
- If the textbook PDF was copied in after setup, `nq init` now adds it (once). If it's still missing, `nq init` warns, and NOVA tells students the textbook hasn't been added instead of calling every question off-syllabus.

## 1.2.1 (19 Oct 2026): Launcher fix for Windows

- `nq up` on a freshly unzipped folder failed to start Postgres on Windows ("The system cannot find the path specified") because the log folder didn't exist yet. The launcher now creates it first.
- `nq up` checks Postgres properly: it says when it's already running, asks you to run `nq init` first when the database hasn't been created, and if Postgres can't start it shows the reason from the log and stops instead of starting the web app with no database.
- `nq status` and `nq down` report Postgres too; if the web app doesn't answer, `nq up` shows the last lines of its log.

## 1.2.0 (19 Oct 2026): Simpler flows

Students
- Home shows one "Up next": the nearest due task, otherwise the next step of the topic you're on, plus at most three other items.
- Every topic is a 4-step path: Learn in pictures → Practise → Prove it → Teach NOVA, with a tick per step and one Continue button. Each step ends by sending you to the next one.
- Learn starts with "Continue where you left off"; chapters show each topic's progress; Learn in pictures keeps its own entry.
- Practice missions and assignments show one question per screen with a progress bar, Back/Next, hints, and a review screen before handing in. The repeated "Mission stage…" text is gone.
- Ask NOVA is a floating button on every learning screen. The textbook text and quick checks sit behind "Read the textbook". The Progress tab is now "Me".

Teachers
- Menu cut from 8 items to 4: Home, Assignments, Classes, Syllabus. The notification bell sits next to the logo.
- Home is a to-do list with one button per row: check answers, release results, approve a draft, chase late work.
- Every assignment shows its stage (Draft → Scheduled → Open → Closed → Released), one main action, and Questions/Results tabs. "Approve all" approves a draft in one click; question details and editing fold away.
- Create assignment: topic search, three main types (the rest under "More types"), and difficulty/number/title folded into Options.
- Classes: one page per class with Students and Progress tabs. The question bank is "Reuse saved questions" under Assignments.

Fixes
- Confirmation messages no longer break links to a section of a page.

## 1.1.0 (17 Oct 2026): Learn in pictures

- New module, Learn in pictures: a picture-first lesson for all 125 topics (419 cards), in simple language, each ending with the crux and a picture quiz. Reached from Learn and from every topic page.
- 46 original labelled science diagrams, with tap-to-highlight labels and a "hide the names" self-test; plus flows, cycles, comparison tables, groups, bar charts of real data, layers and equations.
- Picture quiz answers are checked on the server and count towards mastery; finishing a lesson is recorded. Teachers see completion counts on the syllabus board and can preview any lesson with answers.
- Content is validated on load and in tests (every topic covered, every drawing used, every label pointing at a real part, every quiz answer valid).
- Fixes: elements marked hidden now always stay hidden, even buttons.

## 1.0.3 (17 Oct 2026)

- Students can sign out from every screen: an account button (their initials) next to the bell opens a menu with their name and Sign out. Before, Sign out was only at the bottom of Progress.
- Shared phones: signing out clears answers still queued on the device. If some haven't reached NovaQuest yet, the student is warned first and can reconnect or tap Sign out again to discard them.

## 1.0.2 (17 Oct 2026)

- New Windows prerequisites installer, `NovaQuest-Prerequisites-Setup.exe`: Visual C++ runtime, Python 3.13, portable PostgreSQL 16, Mailpit, Caddy and Poppler, then the app's packages, `.env` and an `nq` command. Downloads are verified (GitHub sha256 digests, Authenticode signatures); re-runs skip finished steps; `-check` reports status; `-with-pgvector` builds pgvector.
- The launcher runs on Windows: process start/stop without Unix signals, `pg_ctl.exe`/`initdb.exe` options, helper programs found through `NQ_BIN_DIR` and `NQ_EXTRA_PATH`, `.env` restricted to the owner with an ACL.
- pgvector is now optional: without it the schema installs and search uses full text; `nq init` adds the embedding column once pgvector is available.

## 1.0.1 (17 Oct 2026)

- New `python -m tools.nq set-key`: saves the Claude API key into `.env` with hidden input and owner-only file permissions, and fills in a random `NQ_SECRET_KEY`.
- `tools/smoke_claude.py` now reads `.env`, so the live check works right after `set-key`.
- Added `.gitignore` so `.env` and the textbook PDF are never committed.

## 1.0.0 (16 Oct 2026): Grade 8 Science MVP

First release, for the CBSE school demo. Grade 8 Science (NCERT, 2018-19 edition, all 18 chapters). PSG MSc track follows.

- Onboarding: institutions, classes, staff invites (form and CSV), teacher mapping, year rollover; students by form or CSV with row-level checks, PIN login slips, PIN reset, parental consent, deactivate and move.
- Syllabus board built from the textbook PDF: 18 chapters, 125 topics with learning outcomes, 436 cited passages, 26 reviewed misconceptions, full lessons for Chapter 8; teacher notes as extra source; syllabus versioning.
- Assignments: AI-guided missions, quizzes, short answers, understanding checks, Learn and Teach NOVA; teacher approval of every question; question bank; class/group/student targets; IST scheduling, time limits, attempts, extensions; states draft → scheduled → open → closed → reviewed.
- Students: phone-first app, autosave with offline queue, graded tutor hints that never give the answer, scorecards after release, parent report and share link, reminders.
- Review: flagged-first queue, AI rationale, overrides, feedback, release gate, completion digests and expiry summaries.
- Learn mode: lessons with adaptive checks, ask-the-tutor with citations, practice missions, understanding checks, mastery levels, adaptive next step, revision plan, teacher topic locks and conversation logs.
- Teach NOVA: AI classmate with planned misconceptions, explicit belief state, evaluator-gated learning, scoring, transcripts, starters and voice input, guardrails.
- Dashboards: teacher results and class mastery, leadership overview, AI-skills section, CSV and PDF exports.
- Platform: Postgres 16 with row-level security and pgvector, hybrid search, Claude API with fast/strong tiers, prompt caching, usage caps and offline fallback, job worker with LISTEN/NOTIFY, Mailpit, Caddy HTTPS, installable PWA.
