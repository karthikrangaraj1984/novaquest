-- 1.30.0: a teacher's weekly timetable, and one piece of work set for several sections at once.

-- When a teacher meets each class for each subject: a day of the week (0 = Monday … 5 = Saturday), a start time and
-- the period's length. Today's page lists these in order; lesson plans count the lessons a chapter gets.
CREATE TABLE timetable_slots (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  teacher_id     bigint NOT NULL REFERENCES users(id),
  class_id       bigint NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
  weekday        smallint NOT NULL CHECK (weekday BETWEEN 0 AND 5),
  starts_at      time NOT NULL,
  minutes        smallint NOT NULL DEFAULT 40 CHECK (minutes BETWEEN 20 AND 120),
  UNIQUE (teacher_id, weekday, starts_at)
);
CREATE INDEX timetable_slots_class ON timetable_slots (class_id, syllabus_id);
ALTER TABLE timetable_slots ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON timetable_slots USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));

-- Work set for several sections: each section gets its own assignment (its own times and results) and they share the
-- same approved questions; batch_id is the first one's id. `sections` holds the other classes a draft is meant for.
ALTER TABLE assignments ADD COLUMN batch_id bigint REFERENCES assignments(id) ON DELETE SET NULL;
ALTER TABLE assignments ADD COLUMN sections bigint[] NOT NULL DEFAULT '{}';
CREATE INDEX assignments_batch ON assignments (batch_id) WHERE batch_id IS NOT NULL;
