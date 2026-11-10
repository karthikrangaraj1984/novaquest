-- 1.31.0: the student's own growth: mistakes to redo, exam mode, questions for the teacher, reflections and goals.

-- A wrong answer in released work stays in "My mistakes" until the student gets that question right on a redo.
CREATE TABLE mistake_fixes (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  question_id    bigint NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  tries          integer NOT NULL DEFAULT 0,
  fixed_at       timestamptz,
  last_at        timestamptz,
  PRIMARY KEY (student_id, question_id)
);

-- Exam mode: an exam the student is preparing for (which chapters, when), and their timed practice papers.
CREATE TABLE exam_plans (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  title          text NOT NULL,
  syllabus_ids   bigint[] NOT NULL,
  chapter_ids    bigint[] NOT NULL,
  exam_on        date NOT NULL,
  created_at     timestamptz NOT NULL
);
CREATE INDEX exam_plans_student ON exam_plans (student_id, exam_on);
CREATE TABLE exam_practice (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  plan_id        bigint NOT NULL REFERENCES exam_plans(id) ON DELETE CASCADE,
  items          jsonb NOT NULL,
  minutes        smallint NOT NULL,
  answers        jsonb,
  score          numeric(4,3),
  started_at     timestamptz NOT NULL,
  done_at        timestamptz
);

-- Ask my teacher: a student's question about a topic or a question they got wrong, and the teacher's reply.
CREATE TABLE doubts (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  syllabus_id    bigint REFERENCES syllabi(id) ON DELETE SET NULL,
  topic_id       bigint REFERENCES syllabus_nodes(id) ON DELETE SET NULL,
  question_id    bigint REFERENCES questions(id) ON DELETE SET NULL,
  assignment_id  bigint REFERENCES assignments(id) ON DELETE SET NULL,
  body           text NOT NULL,
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
  reply          text,
  replied_by     bigint REFERENCES users(id),
  replied_at     timestamptz,
  created_at     timestamptz NOT NULL
);
CREATE INDEX doubts_class ON doubts (class_id, status);

-- How a piece of work felt, and what the student will do next (after its results).
CREATE TABLE reflections (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  feeling        text NOT NULL CHECK (feeling IN ('easy', 'okay', 'hard')),
  next_step      text,
  created_at     timestamptz NOT NULL,
  PRIMARY KEY (student_id, assignment_id)
);

-- A weekly goal the student sets: how many topics to work on this week (Monday to Sunday).
CREATE TABLE weekly_goals (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  week_start     date NOT NULL,
  target         smallint NOT NULL CHECK (target BETWEEN 1 AND 20),
  PRIMARY KEY (student_id, week_start)
);

ALTER TABLE mistake_fixes ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON mistake_fixes USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
ALTER TABLE exam_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON exam_plans USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
ALTER TABLE exam_practice ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON exam_practice USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
ALTER TABLE doubts ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON doubts USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
ALTER TABLE reflections ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON reflections USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
ALTER TABLE weekly_goals ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON weekly_goals USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
