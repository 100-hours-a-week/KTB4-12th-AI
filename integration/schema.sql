-- Apply only to the isolated integration DB, after profiler Alembic migrations.
-- Catalog operator owns ai_catalog; applications never activate/write products.
DO $$ BEGIN
 IF current_user <> 'fixture_owner' THEN
  RAISE EXCEPTION 'Integration schema is restricted to the disposable fixture-owner database';
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS ai_catalog.search_generations (
 catalog_version uuid PRIMARY KEY REFERENCES ai_catalog.catalog_versions(id),
 manifest jsonb NOT NULL, vectors bytea NOT NULL,
 CHECK (manifest->>'ai_catalog_version'=catalog_version::text)
);
CREATE TABLE IF NOT EXISTS ai_catalog.integration_marker(id boolean PRIMARY KEY CHECK(id),run_identity uuid NOT NULL DEFAULT gen_random_uuid());
INSERT INTO ai_catalog.integration_marker(id) VALUES(true) ON CONFLICT DO NOTHING;
ALTER TABLE ai_catalog.products ADD COLUMN IF NOT EXISTS display_image_path text
 CHECK (display_image_path IS NULL OR display_image_path ~ '^/media/[A-Za-z0-9._-]+$');
ALTER TABLE ai_catalog.products ADD COLUMN IF NOT EXISTS display_image_large_path text;
ALTER TABLE ai_catalog.products ADD COLUMN IF NOT EXISTS display_image_fallback_path text;
ALTER TABLE ai_catalog.products ADD COLUMN IF NOT EXISTS search_document text;
ALTER TABLE ai_catalog.products ADD COLUMN IF NOT EXISTS normalized_name text;
CREATE TABLE IF NOT EXISTS ai_catalog.integration_category_sources(
 category_id bigint PRIMARY KEY REFERENCES ai_catalog.categories(id), source_category_id text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_catalog.product_media (
 source_product_id text REFERENCES ai_catalog.products(source_product_id),
 kind text NOT NULL CHECK(kind IN ('representative','additional','detail')),
 ordinal integer NOT NULL, source_url text NOT NULL, media_path text,
 PRIMARY KEY(source_product_id,kind,ordinal)
);
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ai_chat_service') THEN
  CREATE ROLE ai_chat_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
 END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ai_search_service') THEN
  CREATE ROLE ai_search_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
 END IF;
END $$;
-- ai_app is created NOLOGIN by profiler migration0007; local fixture DB only.
ALTER ROLE ai_app LOGIN;
CREATE SCHEMA IF NOT EXISTS ai_chat AUTHORIZATION ai_chat_service;
CREATE SCHEMA IF NOT EXISTS ai_search;
CREATE TABLE IF NOT EXISTS ai_search.searches(id text PRIMARY KEY,created_at text,request text,response text);
CREATE TABLE IF NOT EXISTS ai_search.feedback(id text PRIMARY KEY,search_id text REFERENCES ai_search.searches(id) ON DELETE CASCADE,created_at text,product_id bigint,verdict text,note text);
GRANT USAGE ON SCHEMA ai_search,ai_catalog TO ai_search_service;
GRANT SELECT ON ALL TABLES IN SCHEMA ai_catalog TO ai_search_service,ai_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA ai_search TO ai_search_service;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
