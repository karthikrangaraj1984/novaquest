-- Escape rooms (learn AI through AI): one room per chapter, four locks each. A run is one student's progress
-- through one room; each lock keeps its tries, hints and score, so AI skills can be measured.
CREATE TABLE escape_runs (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  chapter        integer NOT NULL,
  status         text NOT NULL DEFAULT 'playing' CHECK (status IN ('playing', 'escaped')),
  started_at     timestamptz NOT NULL,
  escaped_at     timestamptz,
  UNIQUE (student_id, chapter)
);
CREATE TABLE escape_locks (
  run_id         bigint NOT NULL REFERENCES escape_runs(id) ON DELETE CASCADE,
  lock_no        integer NOT NULL,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  skill          text NOT NULL,
  tries          integer NOT NULL DEFAULT 0,
  hints          integer NOT NULL DEFAULT 0,
  score          numeric(4,2),
  last_answer    jsonb,
  solved_at      timestamptz,
  PRIMARY KEY (run_id, lock_no)
);
CREATE INDEX escape_runs_student_idx ON escape_runs (institution_id, student_id);
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['escape_runs', 'escape_locks']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id))', t);
  END LOOP;
END $$;

-- Opening a lock is a learning event on the lock's topic (it counts as Learning, never as Practised or Mastered).
ALTER TABLE learn_events DROP CONSTRAINT learn_events_kind_check;
ALTER TABLE learn_events ADD CONSTRAINT learn_events_kind_check
  CHECK (kind IN ('lesson_started', 'check', 'practice', 'understanding', 'teach', 'assignment', 'pictures', 'escape'));
