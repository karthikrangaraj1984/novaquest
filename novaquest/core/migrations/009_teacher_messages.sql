-- 1.13.0: a teacher's message to the whole class with an assignment's results, and when each parent was last emailed
-- a progress report.
ALTER TABLE assignments ADD COLUMN class_message text;
ALTER TABLE enrolments ADD COLUMN report_emailed_at timestamptz;
