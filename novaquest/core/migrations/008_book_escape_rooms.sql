-- 1.12.0: escape rooms for every book, and the old NCERT Grade 8 Science (2018) book retired.

-- Escape rooms drafted by Claude for a school's own book: one per chapter, approved by the school admin before
-- students and teachers see it. (The retired NCERT book's rooms stay in data/escape, for its old runs.)
CREATE TABLE escape_rooms (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
  chapter        integer NOT NULL,
  room           jsonb NOT NULL,
  status         text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
  created_at     timestamptz NOT NULL,
  approved_at    timestamptz,
  UNIQUE (syllabus_id, chapter)
);
ALTER TABLE escape_rooms ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON escape_rooms USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));

-- A run belongs to one book's room: (book, chapter). Runs so far were all in the NCERT book.
ALTER TABLE escape_runs ADD COLUMN syllabus_id bigint REFERENCES syllabi(id) ON DELETE CASCADE;
UPDATE escape_runs SET syllabus_id = (SELECT id FROM syllabi WHERE institution_id IS NULL AND board='NCERT'
                                      AND subject='Science' AND grade='8' ORDER BY is_current DESC, id DESC LIMIT 1)
 WHERE syllabus_id IS NULL;
ALTER TABLE escape_runs ALTER COLUMN syllabus_id SET NOT NULL;
ALTER TABLE escape_runs DROP CONSTRAINT escape_runs_student_id_chapter_key;
ALTER TABLE escape_runs ADD CONSTRAINT escape_runs_student_book_chapter_key UNIQUE (student_id, syllabus_id, chapter);

-- The NCERT Class 8 Science book of 2018 is out of date: no school is offered it or studies it any more. Past work
-- (assignments, answers, mastery) keeps pointing at it, so it stays in the database as an archived book, and teachers
-- keep their link to it so they can still open their past assignments (they can't set new work: no class has it).
UPDATE syllabi SET status='archived', is_current=false
 WHERE institution_id IS NULL AND board='NCERT' AND subject='Science' AND grade='8';
DELETE FROM class_subjects WHERE syllabus_id IN (SELECT id FROM syllabi WHERE institution_id IS NULL AND board='NCERT'
                                                 AND subject='Science' AND grade='8');
