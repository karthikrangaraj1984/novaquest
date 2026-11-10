-- Exam papers: a teacher sets the pattern (sections of N questions x M marks), the difficulty and the topics;
-- NovaQuest writes three different sets to choose from; the chosen one prints as a question paper and answer key.
CREATE TABLE exam_papers (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  teacher_id     bigint NOT NULL REFERENCES users(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  title          text NOT NULL,
  pattern        jsonb NOT NULL,
  difficulty     text NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard', 'mixed')),
  topic_ids      bigint[] NOT NULL,
  duration_min   integer NOT NULL,
  instructions   text,
  answer_space   boolean NOT NULL DEFAULT false,
  chosen_set     integer CHECK (chosen_set BETWEEN 1 AND 3),
  created_at     timestamptz NOT NULL,
  updated_at     timestamptz NOT NULL
);
CREATE INDEX exam_papers_teacher_idx ON exam_papers (institution_id, teacher_id, created_at DESC);
CREATE TABLE exam_paper_items (
  paper_id       bigint NOT NULL REFERENCES exam_papers(id) ON DELETE CASCADE,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  set_no         integer NOT NULL CHECK (set_no BETWEEN 1 AND 3),
  section_no     integer NOT NULL,
  item_no        integer NOT NULL,
  question_id    bigint NOT NULL REFERENCES questions(id),
  marks          numeric(4,1) NOT NULL,
  PRIMARY KEY (paper_id, set_no, section_no, item_no)
);
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['exam_papers', 'exam_paper_items']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id))', t);
  END LOOP;
END $$;
