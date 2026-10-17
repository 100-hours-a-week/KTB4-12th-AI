-- Chat migration, executed by the migration role only.

CREATE TABLE IF NOT EXISTS conversations (
 id uuid PRIMARY KEY, sender bigint NOT NULL, recipient bigint NOT NULL,
 created timestamptz NOT NULL DEFAULT now(), touched timestamptz NOT NULL DEFAULT now());
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS demo_profile jsonb;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS protocol_fixture boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS turns (
 id uuid PRIMARY KEY, conversation uuid REFERENCES conversations(id) NOT NULL,
 ordinal bigserial UNIQUE, state jsonb NOT NULL, context jsonb NOT NULL DEFAULT '{}',
 created timestamptz NOT NULL DEFAULT now());
ALTER TABLE turns ADD COLUMN IF NOT EXISTS requested_model text;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS recipient_context jsonb;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS prepared_context jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn ON turns(conversation)
 WHERE state->>'status'='PROCESSING';
CREATE TABLE IF NOT EXISTS events (
 turn uuid REFERENCES turns(id) NOT NULL, seq int NOT NULL, kind text NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(turn,seq));
CREATE TABLE IF NOT EXISTS admission_control (id int PRIMARY KEY CHECK(id=1), paused boolean NOT NULL DEFAULT false);
INSERT INTO admission_control VALUES(1,false) ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION chat_check_admission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (SELECT paused FROM admission_control WHERE id=1 FOR SHARE) THEN
  RAISE EXCEPTION 'chat admissions paused for deployment' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END;$$;
CREATE OR REPLACE TRIGGER chat_admission_guard BEFORE INSERT ON turns
 FOR EACH ROW WHEN (NEW.state->>'status'='PROCESSING') EXECUTE FUNCTION chat_check_admission();
CREATE TABLE IF NOT EXISTS runtime_lease (
 id int PRIMARY KEY CHECK(id=1), owner text NOT NULL, expires timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
 id uuid PRIMARY KEY, turn uuid REFERENCES turns(id) NOT NULL, payload jsonb NOT NULL,
 reviewed boolean NOT NULL DEFAULT false, created timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS task_budget_accounts (
 id text PRIMARY KEY, limit_usd numeric NOT NULL CHECK(limit_usd>0 AND limit_usd<=5),
 imported_usd numeric NOT NULL CHECK(imported_usd>=0), import_hash text NOT NULL,
 halted text, created timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS task_budget_attempts (
 id uuid PRIMARY KEY, account text REFERENCES task_budget_accounts(id) NOT NULL,
 reserved_usd numeric NOT NULL CHECK(reserved_usd>0), actual_usd numeric CHECK(actual_usd>=0),
 model text NOT NULL, status text NOT NULL DEFAULT 'reserved',
 created timestamptz NOT NULL DEFAULT now(), finished timestamptz);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS conversation_reports (
 id uuid PRIMARY KEY, conversation uuid REFERENCES conversations(id) NOT NULL,
 payload jsonb NOT NULL, context jsonb NOT NULL DEFAULT '{}', reviewed boolean NOT NULL DEFAULT false,
 created timestamptz NOT NULL DEFAULT now());
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS workflow_status text;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS workflow_updated timestamptz;
ALTER TABLE conversation_reports ADD COLUMN IF NOT EXISTS workflow_status text;
ALTER TABLE conversation_reports ADD COLUMN IF NOT EXISTS workflow_updated timestamptz;
CREATE TABLE IF NOT EXISTS report_reviews (
 id uuid PRIMARY KEY, report_id uuid NOT NULL, scope text NOT NULL,
 status text NOT NULL, note text NOT NULL, evidence jsonb,
 created timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS report_reviews_lookup ON report_reviews(report_id,created);
CREATE TABLE IF NOT EXISTS sessions (
 id uuid PRIMARY KEY, sender bigint NOT NULL, created timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS schema_version (singleton boolean PRIMARY KEY CHECK(singleton), version text NOT NULL);
INSERT INTO schema_version VALUES(true,'single-server/1') ON CONFLICT(singleton) DO UPDATE SET version=excluded.version;
