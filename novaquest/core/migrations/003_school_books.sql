-- School books (SY-5, SY-6): a school uploads its own textbooks (any board, subject, grade); each class studies one
-- or more of them. Shared books (institution_id NULL, e.g. NCERT) stay readable by every school; a school's own
-- books, their topics, lessons and passages are visible only to that school (row-level security).

CREATE OR REPLACE FUNCTION nq_shared_ok(inst bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT inst IS NULL OR nq_tenant_ok(inst) $$;

CREATE OR REPLACE FUNCTION nq_shared_write_ok(inst bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT (inst IS NULL AND coalesce(current_setting('nq.platform', true), '') = 'on')
      OR (inst IS NOT NULL AND nq_tenant_ok(inst))
$$;

-- ---------- syllabi owned by a school ----------
ALTER TABLE syllabi ADD COLUMN institution_id bigint REFERENCES institutions(id);
ALTER TABLE syllabi ADD COLUMN status text NOT NULL DEFAULT 'published'
  CHECK (status IN ('processing', 'review', 'published', 'archived', 'failed'));
ALTER TABLE syllabi ADD COLUMN created_by bigint REFERENCES users(id);
ALTER TABLE syllabi ADD COLUMN meta jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE syllabi DROP CONSTRAINT syllabi_board_subject_grade_version_key;
CREATE UNIQUE INDEX syllabi_unique_version ON syllabi (coalesce(institution_id, 0), board, subject, grade, version);
CREATE INDEX syllabi_inst_idx ON syllabi (institution_id);

ALTER TABLE syllabus_nodes ADD COLUMN institution_id bigint REFERENCES institutions(id);
ALTER TABLE lessons ADD COLUMN institution_id bigint REFERENCES institutions(id);
ALTER TABLE misconceptions ADD COLUMN institution_id bigint REFERENCES institutions(id);
ALTER TABLE misconceptions ADD COLUMN status text NOT NULL DEFAULT 'approved' CHECK (status IN ('draft', 'approved'));
CREATE INDEX syllabus_nodes_syllabus_idx ON syllabus_nodes (syllabus_id, kind);

-- Picture lessons drafted for a school's own book (the NCERT ones ship as files in data/pictures).
CREATE TABLE picture_lessons (
  node_id        bigint PRIMARY KEY REFERENCES syllabus_nodes(id) ON DELETE CASCADE,
  institution_id bigint REFERENCES institutions(id),
  data           jsonb NOT NULL,
  status         text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The uploaded PDF files of a school's book (kept so the book can be re-read).
CREATE TABLE book_files (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
  filename       text NOT NULL,
  stored_as      text NOT NULL,
  sha256         text NOT NULL,
  bytes          bigint NOT NULL,
  pages          integer,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX book_files_syllabus_idx ON book_files (syllabus_id);

-- Which books (subjects) each class studies.
CREATE TABLE class_subjects (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  ord            integer NOT NULL DEFAULT 0,
  PRIMARY KEY (class_id, syllabus_id)
);

-- Every existing class keeps studying the shared NCERT Science book it used before.
INSERT INTO class_subjects (institution_id, class_id, syllabus_id)
SELECT c.institution_id, c.id, s.id FROM classes c
JOIN syllabi s ON s.institution_id IS NULL AND s.is_current AND s.board = 'NCERT' AND s.subject = 'Science' AND s.grade = '8';

-- ---------- row-level security ----------
ALTER TABLE syllabi ENABLE ROW LEVEL SECURITY;
CREATE POLICY syllabi_read ON syllabi FOR SELECT USING (nq_shared_ok(institution_id));
CREATE POLICY syllabi_insert ON syllabi FOR INSERT WITH CHECK (nq_shared_write_ok(institution_id));
CREATE POLICY syllabi_update ON syllabi FOR UPDATE USING (nq_shared_write_ok(institution_id)) WITH CHECK (nq_shared_write_ok(institution_id));
CREATE POLICY syllabi_delete ON syllabi FOR DELETE USING (nq_shared_write_ok(institution_id));

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['syllabus_nodes', 'lessons', 'misconceptions', 'picture_lessons']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY shared_read ON %I FOR SELECT USING (nq_shared_ok(institution_id))', t);
    EXECUTE format('CREATE POLICY shared_insert ON %I FOR INSERT WITH CHECK (nq_shared_write_ok(institution_id))', t);
    EXECUTE format('CREATE POLICY shared_update ON %I FOR UPDATE USING (nq_shared_write_ok(institution_id)) WITH CHECK (nq_shared_write_ok(institution_id))', t);
    EXECUTE format('CREATE POLICY shared_delete ON %I FOR DELETE USING (nq_shared_write_ok(institution_id))', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['book_files', 'class_subjects']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id))', t);
  END LOOP;
END $$;
