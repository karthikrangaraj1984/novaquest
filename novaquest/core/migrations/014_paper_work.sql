-- 1.21.0: marking work done on paper. A teacher photographs a student's answers; Claude reads the handwriting (or
-- the teacher types it); NovaQuest marks it like work handed in online. The photos are files in the data folder.
CREATE TABLE paper_scans (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  student_id     bigint NOT NULL REFERENCES users(id),
  pages          integer NOT NULL,
  status         text NOT NULL CHECK (status IN ('reading', 'to_check', 'marked', 'failed')),
  reading        jsonb NOT NULL DEFAULT '{}'::jsonb,
  read_by        text CHECK (read_by IN ('claude', 'teacher')),
  error          text,
  attempt_id     bigint REFERENCES attempts(id) ON DELETE SET NULL,
  uploaded_by    bigint NOT NULL REFERENCES users(id),
  created_at     timestamptz NOT NULL,
  updated_at     timestamptz NOT NULL,
  UNIQUE (assignment_id, student_id)
);
ALTER TABLE paper_scans ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON paper_scans USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));

ALTER TABLE attempts ADD COLUMN source text NOT NULL DEFAULT 'online' CHECK (source IN ('online', 'paper'));
