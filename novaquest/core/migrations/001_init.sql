-- NovaQuest schema v1. Applied by novaquest.core.migrate as the owner role.
-- Every institution-owned table carries institution_id and is protected by row-level security:
-- the app role sees only rows of the institution set in the transaction (SET LOCAL nq.institution_id).

-- pgvector is optional (created by bootstrap when the server has it); the embedding column is added
-- by novaquest.core.migrate once it is enabled.
CREATE EXTENSION IF NOT EXISTS ltree;

CREATE OR REPLACE FUNCTION nq_tenant_ok(inst bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('nq.platform', true), '') = 'on'
      OR inst = nullif(current_setting('nq.institution_id', true), '')::bigint
$$;

-- ---------- institutions and people ----------
CREATE TABLE institutions (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name          text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('school', 'college')),
  board         text NOT NULL,
  academic_year text NOT NULL,
  school_code   text NOT NULL UNIQUE,
  ai_daily_token_cap integer NOT NULL DEFAULT 2000000,
  status        text NOT NULL DEFAULT 'active',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint REFERENCES institutions(id),
  name           text NOT NULL,
  email          text UNIQUE,
  password_hash  text,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invited', 'inactive')),
  invite_token_hash text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz
);
CREATE INDEX users_inst_idx ON users(institution_id);

CREATE TABLE user_roles (
  user_id        bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  institution_id bigint REFERENCES institutions(id),
  role           text NOT NULL CHECK (role IN ('platform_admin', 'inst_admin', 'leader', 'teacher', 'student')),
  PRIMARY KEY (user_id, role)
);
CREATE INDEX user_roles_inst_idx ON user_roles(institution_id, role);

CREATE TABLE classes (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  label          text NOT NULL,
  grade          text NOT NULL,
  section        text NOT NULL,
  academic_year  text NOT NULL,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (institution_id, label, academic_year)
);

CREATE TABLE enrolments (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  roll_no        text NOT NULL,
  pin_hash       text NOT NULL,
  parent_email   text,
  consent_status text NOT NULL DEFAULT 'pending' CHECK (consent_status IN ('pending', 'recorded')),
  consent_by     text,
  consent_method text,
  consent_at     timestamptz,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'moved', 'inactive', 'promoted')),
  started_at     timestamptz NOT NULL DEFAULT now(),
  ended_at       timestamptz
);
CREATE UNIQUE INDEX enrolments_roll_active ON enrolments(class_id, roll_no) WHERE status = 'active';
CREATE INDEX enrolments_student_idx ON enrolments(student_id);
CREATE INDEX enrolments_class_idx ON enrolments(class_id, status);

-- ---------- syllabus (shared across institutions) ----------
CREATE TABLE syllabi (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  board       text NOT NULL,
  subject     text NOT NULL,
  grade       text NOT NULL,
  version     text NOT NULL,
  title       text NOT NULL,
  is_current  boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (board, subject, grade, version)
);

CREATE TABLE syllabus_nodes (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  syllabus_id bigint NOT NULL REFERENCES syllabi(id),
  parent_id   bigint REFERENCES syllabus_nodes(id),
  kind        text NOT NULL CHECK (kind IN ('chapter', 'topic', 'outcome')),
  code        text NOT NULL,
  title       text NOT NULL,
  ord         integer NOT NULL,
  path        ltree NOT NULL,
  meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (syllabus_id, kind, code)
);
CREATE INDEX syllabus_nodes_path_idx ON syllabus_nodes USING gist (path);
CREATE INDEX syllabus_nodes_parent_idx ON syllabus_nodes(parent_id);

CREATE TABLE source_passages (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  node_id        bigint NOT NULL REFERENCES syllabus_nodes(id),
  institution_id bigint REFERENCES institutions(id),
  source_kind    text NOT NULL DEFAULT 'textbook' CHECK (source_kind IN ('textbook', 'teacher_notes')),
  ref            text NOT NULL,
  page           integer,
  body           text NOT NULL,
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX source_passages_tsv_idx ON source_passages USING gin (tsv);
CREATE INDEX source_passages_node_idx ON source_passages(node_id);

CREATE TABLE misconceptions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  syllabus_id bigint NOT NULL REFERENCES syllabi(id),
  code        text NOT NULL,
  topic_id    bigint NOT NULL REFERENCES syllabus_nodes(id),
  chapter_id  bigint NOT NULL REFERENCES syllabus_nodes(id),
  label       text NOT NULL,
  data        jsonb NOT NULL,
  UNIQUE (syllabus_id, code)
);

CREATE TABLE lessons (
  node_id     bigint PRIMARY KEY REFERENCES syllabus_nodes(id),
  data        jsonb NOT NULL,
  status      text NOT NULL DEFAULT 'approved'
);

-- ---------- teaching structure ----------
CREATE TABLE teaching_assignments (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  teacher_id     bigint NOT NULL REFERENCES users(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  UNIQUE (teacher_id, class_id, syllabus_id)
);

CREATE TABLE student_groups (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  name           text NOT NULL
);

CREATE TABLE group_members (
  group_id       bigint NOT NULL REFERENCES student_groups(id) ON DELETE CASCADE,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  PRIMARY KEY (group_id, student_id)
);

CREATE TABLE topic_settings (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  node_id        bigint NOT NULL REFERENCES syllabus_nodes(id),
  open_for_self_study boolean NOT NULL DEFAULT true,
  PRIMARY KEY (class_id, node_id)
);

-- ---------- questions and assignments ----------
CREATE TABLE questions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  topic_id       bigint NOT NULL REFERENCES syllabus_nodes(id),
  kind           text NOT NULL CHECK (kind IN ('mcq', 'short', 'viva', 'ai_moment', 'teach', 'learn')),
  prompt         text NOT NULL,
  scenario       text,
  options        jsonb,
  answer         jsonb,
  rubric         jsonb,
  difficulty     text NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
  thinking_level text NOT NULL CHECK (thinking_level IN ('remember', 'apply', 'analyse')),
  outcome        text NOT NULL,
  ai_concept     text,
  misconception_codes text[] NOT NULL DEFAULT '{}',
  citation       jsonb NOT NULL DEFAULT '[]'::jsonb,
  status         text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'removed')),
  origin         text NOT NULL DEFAULT 'ai' CHECK (origin IN ('ai', 'bank', 'teacher')),
  created_by     bigint REFERENCES users(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX questions_bank_idx ON questions(institution_id, topic_id, status);

CREATE TABLE assignments (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  class_id       bigint NOT NULL REFERENCES classes(id),
  teacher_id     bigint NOT NULL REFERENCES users(id),
  syllabus_id    bigint NOT NULL REFERENCES syllabi(id),
  title          text NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('mission', 'quiz', 'short', 'viva', 'learn', 'teach')),
  difficulty     text NOT NULL,
  topic_ids      bigint[] NOT NULL,
  state          text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'scheduled', 'open', 'closed', 'reviewed')),
  opens_at       timestamptz,
  expires_at     timestamptz,
  time_limit_min integer,
  max_attempts   integer NOT NULL DEFAULT 1,
  released_at    timestamptz,
  closed_notified_at timestamptz,
  reminder_sent_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assignments_class_idx ON assignments(institution_id, class_id, state);
CREATE INDEX assignments_due_idx ON assignments(state, expires_at);

CREATE TABLE assignment_questions (
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  question_id    bigint NOT NULL REFERENCES questions(id),
  institution_id bigint NOT NULL REFERENCES institutions(id),
  ord            integer NOT NULL,
  PRIMARY KEY (assignment_id, question_id)
);

CREATE TABLE assignment_targets (
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  target_kind    text NOT NULL CHECK (target_kind IN ('class', 'group', 'student')),
  target_id      bigint NOT NULL,
  PRIMARY KEY (assignment_id, target_kind, target_id)
);

CREATE TABLE assignment_extensions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  expires_at     timestamptz NOT NULL,
  UNIQUE (assignment_id, student_id)
);

CREATE TABLE attempts (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  assignment_id  bigint NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  student_id     bigint NOT NULL REFERENCES users(id),
  attempt_no     integer NOT NULL,
  started_at     timestamptz NOT NULL,
  deadline_at    timestamptz NOT NULL,
  submitted_at   timestamptz,
  status         text NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'submitted', 'expired')),
  feedback       text,
  UNIQUE (assignment_id, student_id, attempt_no)
);
CREATE INDEX attempts_student_idx ON attempts(student_id, status);

CREATE TABLE responses (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  attempt_id     bigint NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  question_id    bigint NOT NULL REFERENCES questions(id),
  answer         jsonb,
  saved_at       timestamptz NOT NULL,
  ai_score       numeric(5,2),
  max_score      numeric(5,2),
  ai_rationale   text,
  confidence     numeric(3,2),
  flagged        boolean NOT NULL DEFAULT false,
  misconception_codes text[] NOT NULL DEFAULT '{}',
  teacher_score  numeric(5,2),
  teacher_comment text,
  reviewed_by    bigint REFERENCES users(id),
  reviewed_at    timestamptz,
  UNIQUE (attempt_id, question_id)
);
CREATE INDEX responses_flag_idx ON responses(institution_id, flagged);

CREATE TABLE tutor_messages (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  context        text NOT NULL CHECK (context IN ('assignment', 'learn')),
  attempt_id     bigint REFERENCES attempts(id) ON DELETE CASCADE,
  question_id    bigint REFERENCES questions(id),
  topic_id       bigint REFERENCES syllabus_nodes(id),
  role           text NOT NULL CHECK (role IN ('student', 'tutor')),
  body           text NOT NULL,
  hint_level     integer,
  citation       jsonb,
  created_at     timestamptz NOT NULL
);
CREATE INDEX tutor_messages_student_idx ON tutor_messages(institution_id, student_id, created_at);

-- ---------- learning progress ----------
CREATE TABLE learn_events (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  topic_id       bigint NOT NULL REFERENCES syllabus_nodes(id),
  kind           text NOT NULL CHECK (kind IN ('lesson_started', 'check', 'practice', 'understanding', 'teach', 'assignment')),
  score          numeric(4,3),
  data           jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL
);
CREATE INDEX learn_events_student_topic_idx ON learn_events(student_id, topic_id);

CREATE TABLE mastery (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  topic_id       bigint NOT NULL REFERENCES syllabus_nodes(id),
  level          smallint NOT NULL CHECK (level BETWEEN 0 AND 3),
  taught_ok      boolean NOT NULL DEFAULT false,
  updated_at     timestamptz NOT NULL,
  PRIMARY KEY (student_id, topic_id)
);
CREATE INDEX mastery_inst_topic_idx ON mastery(institution_id, topic_id);

CREATE TABLE teach_sessions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  topic_id       bigint NOT NULL REFERENCES syllabus_nodes(id),
  attempt_id     bigint REFERENCES attempts(id) ON DELETE SET NULL,
  belief         jsonb NOT NULL,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'passed', 'ended')),
  scores         jsonb,
  summary        text,
  started_at     timestamptz NOT NULL,
  ended_at       timestamptz
);
CREATE INDEX teach_sessions_student_idx ON teach_sessions(institution_id, student_id);

CREATE TABLE teach_turns (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  session_id     bigint NOT NULL REFERENCES teach_sessions(id) ON DELETE CASCADE,
  role           text NOT NULL CHECK (role IN ('student', 'nova')),
  body           text NOT NULL,
  verdict        text,
  target_code    text,
  created_at     timestamptz NOT NULL
);
CREATE INDEX teach_turns_session_idx ON teach_turns(session_id);

-- ---------- communication, audit, usage ----------
CREATE TABLE notifications (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  user_id        bigint NOT NULL REFERENCES users(id),
  kind           text NOT NULL,
  title          text NOT NULL,
  body           text NOT NULL,
  link           text,
  dedupe_key     text NOT NULL,
  read_at        timestamptz,
  created_at     timestamptz NOT NULL,
  updated_at     timestamptz NOT NULL,
  UNIQUE (user_id, dedupe_key)
);
CREATE INDEX notifications_user_idx ON notifications(user_id, read_at);

CREATE TABLE email_outbox (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  to_addr        text NOT NULL,
  subject        text NOT NULL,
  body           text NOT NULL,
  dedupe_key     text NOT NULL UNIQUE,
  send_after     timestamptz NOT NULL,
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  attempts       integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL,
  sent_at        timestamptz
);
CREATE INDEX email_outbox_pending_idx ON email_outbox(status, send_after);

CREATE TABLE audit_events (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  institution_id bigint REFERENCES institutions(id),
  actor_id       bigint,
  action         text NOT NULL,
  target         text,
  detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL
);
CREATE INDEX audit_events_inst_idx ON audit_events(institution_id, created_at);

CREATE TABLE ai_usage (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  day            date NOT NULL,
  tier           text NOT NULL,
  calls          integer NOT NULL DEFAULT 0,
  input_tokens   bigint NOT NULL DEFAULT 0,
  output_tokens  bigint NOT NULL DEFAULT 0,
  cache_read_tokens bigint NOT NULL DEFAULT 0,
  fallbacks      integer NOT NULL DEFAULT 0,
  PRIMARY KEY (institution_id, day, tier)
);

CREATE TABLE report_links (
  token_hash     text PRIMARY KEY,
  institution_id bigint NOT NULL REFERENCES institutions(id),
  student_id     bigint NOT NULL REFERENCES users(id),
  created_by     bigint NOT NULL REFERENCES users(id),
  expires_at     timestamptz NOT NULL
);

-- ---------- platform tables (no tenant data) ----------
CREATE TABLE sessions (
  token_hash     text PRIMARY KEY,
  user_id        bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  institution_id bigint,
  active_role    text NOT NULL,
  csrf_token     text NOT NULL,
  created_at     timestamptz NOT NULL,
  expires_at     timestamptz NOT NULL,
  revoked        boolean NOT NULL DEFAULT false
);
CREATE INDEX sessions_user_idx ON sessions(user_id);

CREATE TABLE login_failures (
  login_key      text PRIMARY KEY,
  failures       integer NOT NULL,
  locked_until   timestamptz,
  updated_at     timestamptz NOT NULL
);

CREATE TABLE jobs (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind           text NOT NULL,
  institution_id bigint,
  payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
  run_at         timestamptz NOT NULL,
  status         text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts       integer NOT NULL DEFAULT 0,
  last_error     text,
  dedupe_key     text UNIQUE,
  created_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz
);
CREATE INDEX jobs_ready_idx ON jobs(status, run_at);

-- ---------- login lookups that must cross tenants (narrow, read-only) ----------
CREATE OR REPLACE FUNCTION nq_institution_by_code(code text) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT id FROM institutions WHERE upper(school_code) = upper(code) AND status = 'active'
$$;

CREATE OR REPLACE FUNCTION nq_user_by_email(addr text)
RETURNS TABLE (user_id bigint, institution_id bigint, password_hash text, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT id, institution_id, password_hash, status FROM users WHERE lower(email) = lower(addr)
$$;

CREATE OR REPLACE FUNCTION nq_user_by_invite(token_hash text)
RETURNS TABLE (user_id bigint, institution_id bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT id, institution_id FROM users WHERE invite_token_hash = token_hash AND status = 'invited'
$$;

CREATE OR REPLACE FUNCTION nq_report_link(th text)
RETURNS TABLE (institution_id bigint, student_id bigint, expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT institution_id, student_id, expires_at FROM report_links WHERE token_hash = th
$$;

-- One round trip per request to load a live session with the user's name and roles.
CREATE OR REPLACE FUNCTION nq_session(th text, at timestamptz)
RETURNS TABLE (user_id bigint, institution_id bigint, active_role text, csrf_token text, name text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT s.user_id, s.institution_id, s.active_role, s.csrf_token, u.name,
         ARRAY(SELECT r.role FROM user_roles r WHERE r.user_id = s.user_id)
  FROM sessions s JOIN users u ON u.id = s.user_id
  WHERE s.token_hash = th AND NOT s.revoked AND s.expires_at > at AND u.status = 'active'
$$;

-- ---------- row-level security ----------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['users', 'user_roles', 'classes', 'enrolments', 'teaching_assignments',
      'student_groups', 'group_members', 'topic_settings', 'questions', 'assignments',
      'assignment_questions', 'assignment_targets', 'assignment_extensions', 'attempts', 'responses',
      'tutor_messages', 'learn_events', 'mastery', 'teach_sessions', 'teach_turns', 'notifications',
      'email_outbox', 'audit_events', 'ai_usage', 'report_links']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id))', t);
  END LOOP;
END $$;

ALTER TABLE institutions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON institutions USING (nq_tenant_ok(id)) WITH CHECK (nq_tenant_ok(id));

-- Shared syllabus passages are readable by all; teacher notes only by their institution.
ALTER TABLE source_passages ENABLE ROW LEVEL SECURITY;
CREATE POLICY passages_read ON source_passages FOR SELECT
  USING (institution_id IS NULL OR nq_tenant_ok(institution_id));
CREATE POLICY passages_write ON source_passages FOR INSERT
  WITH CHECK ((institution_id IS NULL AND coalesce(current_setting('nq.platform', true), '') = 'on')
              OR (institution_id IS NOT NULL AND nq_tenant_ok(institution_id)));
CREATE POLICY passages_update ON source_passages FOR UPDATE
  USING (coalesce(current_setting('nq.platform', true), '') = 'on' OR (institution_id IS NOT NULL AND nq_tenant_ok(institution_id)));
CREATE POLICY passages_delete ON source_passages FOR DELETE
  USING (coalesce(current_setting('nq.platform', true), '') = 'on' OR (institution_id IS NOT NULL AND nq_tenant_ok(institution_id)));
