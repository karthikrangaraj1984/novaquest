-- 1.10.11: who wrote each AI question. 'claude' or 'offline' (Claude was configured but couldn't write it, so the
-- offline writer did: the teacher's draft marks it 'Basic' and offers a rewrite). NULL for older questions and for
-- installs without Claude.
ALTER TABLE questions ADD COLUMN IF NOT EXISTS written_by text CHECK (written_by IN ('claude', 'offline'));
