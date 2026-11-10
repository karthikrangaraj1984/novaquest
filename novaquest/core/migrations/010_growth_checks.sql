-- 1.16.0: start-of-term and progress checks. A start-of-term check ('baseline') is a quiz across a book's chapters;
-- each later progress check covers the same topics with fresh questions, so a class's growth can be shown.
ALTER TABLE assignments ADD COLUMN purpose text CHECK (purpose IN ('baseline', 'progress'));
ALTER TABLE assignments ADD COLUMN baseline_id bigint REFERENCES assignments(id);
CREATE INDEX assignments_purpose_idx ON assignments (institution_id, class_id, syllabus_id, purpose) WHERE purpose IS NOT NULL;
