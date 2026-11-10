-- Term plan (teacher review, item 12): when a class studies each chapter of each of its books, so teachers can see
-- whether the class is on track and students see what's being taught in class now.
CREATE TABLE term_plans (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
  chapter_id     bigint NOT NULL REFERENCES syllabus_nodes(id) ON DELETE CASCADE,
  starts_on      date NOT NULL,
  ends_on        date NOT NULL,
  updated_by     bigint REFERENCES users(id),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (class_id, chapter_id),
  CHECK (ends_on >= starts_on)
);
CREATE INDEX term_plans_class_idx ON term_plans (class_id, syllabus_id, starts_on);
ALTER TABLE term_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON term_plans USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
