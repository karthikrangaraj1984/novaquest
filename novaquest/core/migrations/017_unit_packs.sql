-- 1.26.0: unit prep packs. For each chapter a school teaches, a teacher's pack (lesson plan by topic, mix-ups to watch
-- for, the book's activities as demos, key terms) written by Claude and kept so it is written once per school. Without
-- Claude the pack is built from the book on the fly and nothing is stored. Each teacher's own notes on a chapter are kept.
CREATE TABLE unit_packs (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  chapter_id     bigint NOT NULL REFERENCES syllabus_nodes(id) ON DELETE CASCADE,
  status         text NOT NULL CHECK (status IN ('writing', 'ready', 'failed')),
  data           jsonb,
  error          text,
  requested_by   bigint REFERENCES users(id),
  updated_at     timestamptz NOT NULL,
  PRIMARY KEY (institution_id, chapter_id)
);
ALTER TABLE unit_packs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON unit_packs USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));

CREATE TABLE unit_notes (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  teacher_id     bigint NOT NULL REFERENCES users(id),
  chapter_id     bigint NOT NULL REFERENCES syllabus_nodes(id) ON DELETE CASCADE,
  body           text NOT NULL,
  updated_at     timestamptz NOT NULL,
  PRIMARY KEY (teacher_id, chapter_id)
);
ALTER TABLE unit_notes ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON unit_notes USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
