-- 1.28.0: open chapters, and escape rooms as assignments.
-- How a class's book starts for self-study: 'open' (every topic, as before), 'closed' (only what the teacher opens)
-- or 'plan' (each chapter opens on its start date in the term plan). A topic the teacher opened or locked by hand
-- (topic_settings) always wins.
CREATE TABLE class_self_study (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
  mode           text NOT NULL CHECK (mode IN ('open', 'closed', 'plan')),
  updated_by     bigint REFERENCES users(id),
  updated_at     timestamptz NOT NULL,
  PRIMARY KEY (class_id, syllabus_id)
);
ALTER TABLE class_self_study ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON class_self_study USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));

-- An escape room set as an assignment: one 'escape' item, done when the student escapes.
ALTER TABLE questions DROP CONSTRAINT questions_kind_check;
ALTER TABLE questions ADD CONSTRAINT questions_kind_check
  CHECK (kind IN ('mcq', 'short', 'viva', 'ai_moment', 'teach', 'learn', 'numeric', 'escape'));
ALTER TABLE assignments DROP CONSTRAINT assignments_kind_check;
ALTER TABLE assignments ADD CONSTRAINT assignments_kind_check
  CHECK (kind IN ('mission', 'quiz', 'short', 'viva', 'learn', 'teach', 'numeric', 'escape'));
