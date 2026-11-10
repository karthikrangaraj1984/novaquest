-- 1.20.0: number problems (maths and science): questions whose answer is a number, with a unit, a tolerance, step
-- hints, a worked solution and, optionally, each student's own numbers. The key lives in questions.answer.
ALTER TABLE questions DROP CONSTRAINT questions_kind_check;
ALTER TABLE questions ADD CONSTRAINT questions_kind_check
  CHECK (kind IN ('mcq', 'short', 'viva', 'ai_moment', 'teach', 'learn', 'numeric'));
ALTER TABLE assignments DROP CONSTRAINT assignments_kind_check;
ALTER TABLE assignments ADD CONSTRAINT assignments_kind_check
  CHECK (kind IN ('mission', 'quiz', 'short', 'viva', 'learn', 'teach', 'numeric'));
