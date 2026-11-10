-- 1.22.0: each student's language, and translations kept so each sentence is translated once.
ALTER TABLE users ADD COLUMN lang text NOT NULL DEFAULT 'en'
  CHECK (lang IN ('en', 'hi', 'ta', 'te', 'kn', 'ml', 'mr', 'bn', 'gu'));

CREATE TABLE translations (
  institution_id bigint NOT NULL REFERENCES institutions(id),
  lang           text NOT NULL,
  src_hash       text NOT NULL,
  text           text NOT NULL,
  PRIMARY KEY (institution_id, lang, src_hash)
);
ALTER TABLE translations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON translations USING (nq_tenant_ok(institution_id)) WITH CHECK (nq_tenant_ok(institution_id));
