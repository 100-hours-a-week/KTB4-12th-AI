-- Standalone Chat schema preparation in an already provisioned candidate DB.
-- psql service=<existing migration service> -X -v ON_ERROR_STOP=1
--   -v expected_database=emmet4930_<name> -f sql/migrate.sql
\set ON_ERROR_STOP on
SELECT current_database() = :'expected_database'
   AND current_database() LIKE 'emmet4930\_%' ESCAPE '\'
   AND current_user = 'ai_migrator' AS target_ok \gset
\if :target_ok
\else
  \echo 'Wrong candidate database or migration role'
  \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(367109731501);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ai_chat_service'
                 AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole) THEN
    RAISE EXCEPTION 'Existing nonprivileged Chat role required';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
             WHERE n.nspname='ai_chat' AND r.rolname<>'ai_migrator') THEN
    RAISE EXCEPTION 'Existing Chat schema requires explicit ownership migration';
  END IF;
  IF to_regclass('ai_chat.schema_version') IS NOT NULL THEN
    IF EXISTS (SELECT 1 FROM ai_chat.schema_version WHERE version<>'single-server/1') THEN
      RAISE EXCEPTION 'Unexpected Chat schema version';
    END IF;
  END IF;
  IF to_regclass('ai_chat.runtime_lease') IS NOT NULL THEN
    IF EXISTS (SELECT 1 FROM ai_chat.runtime_lease WHERE expires>now()) THEN
      RAISE EXCEPTION 'Stop this candidate Chat before schema migration';
    END IF;
  END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS ai_chat AUTHORIZATION ai_migrator;
SET LOCAL search_path=ai_chat,public;
\ir state.sql
REVOKE CREATE ON SCHEMA ai_chat FROM PUBLIC,ai_chat_service;
GRANT USAGE ON SCHEMA ai_chat TO ai_chat_service;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA ai_chat TO ai_chat_service;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA ai_chat TO ai_chat_service;
REVOKE INSERT,UPDATE,DELETE ON ai_chat.schema_version FROM ai_chat_service;
REVOKE UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON ai_chat.turn_reviews FROM ai_chat_service;
COMMIT;
