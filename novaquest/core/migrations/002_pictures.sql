-- Learn in pictures (LN-10): finishing a picture lesson is recorded as learning evidence.
ALTER TABLE learn_events DROP CONSTRAINT learn_events_kind_check;
ALTER TABLE learn_events ADD CONSTRAINT learn_events_kind_check
  CHECK (kind IN ('lesson_started', 'check', 'practice', 'understanding', 'teach', 'assignment', 'pictures'));
