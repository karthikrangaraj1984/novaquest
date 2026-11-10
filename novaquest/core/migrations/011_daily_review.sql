-- 1.17.0: Daily 5. Each student has a review schedule per topic (spaced repetition: a right answer pushes the next
-- review further away, a wrong one brings it back to tomorrow) and one short review a day.
CREATE TABLE review_items (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  topic_id       bigint NOT NULL REFERENCES syllabus_nodes(id) ON DELETE CASCADE,
  box            smallint NOT NULL DEFAULT 0,
  due_on         date NOT NULL,
  last_at        timestamptz,
  PRIMARY KEY (student_id, topic_id)
);
CREATE TABLE daily_reviews (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  day            date NOT NULL,
  items          jsonb NOT NULL,
  answers        jsonb,
  score          numeric(4,3),
  done_at        timestamptz,
  UNIQUE (student_id, day)
);
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['review_items', 'daily_reviews']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id))', t);
  END LOOP;
END $$;
ALTER TABLE learn_events DROP CONSTRAINT learn_events_kind_check;
ALTER TABLE learn_events ADD CONSTRAINT learn_events_kind_check
  CHECK (kind IN ('lesson_started', 'check', 'practice', 'understanding', 'teach', 'assignment', 'pictures', 'escape', 'review'));
