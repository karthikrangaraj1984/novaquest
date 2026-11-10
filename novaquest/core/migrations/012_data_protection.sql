-- 1.19.0: data protection. How long the school keeps AI conversations and the data of students who have left; a
-- student's record can be erased; parental consent can be withdrawn.
ALTER TABLE institutions ADD COLUMN retention jsonb NOT NULL
  DEFAULT '{"ai_chat_months": 12, "left_student_months": 24}'::jsonb;
ALTER TABLE institutions ADD COLUMN retention_ran_on date;

ALTER TABLE users ADD COLUMN erased_at timestamptz;

ALTER TABLE enrolments DROP CONSTRAINT enrolments_consent_status_check;
ALTER TABLE enrolments ADD CONSTRAINT enrolments_consent_status_check
  CHECK (consent_status IN ('pending', 'recorded', 'withdrawn'));
ALTER TABLE enrolments ADD COLUMN consent_withdrawn_at timestamptz;

-- The AI conversation log and the clean-up both read conversations by date.
CREATE INDEX IF NOT EXISTS tutor_messages_created ON tutor_messages (institution_id, created_at);
CREATE INDEX IF NOT EXISTS teach_turns_created ON teach_turns (institution_id, created_at);
