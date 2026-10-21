-- Applied by the migration role, never by a serving application.
CREATE SCHEMA IF NOT EXISTS ai_catalog;
CREATE TABLE ai_catalog.generations (
 id uuid PRIMARY KEY,
 export_id uuid NOT NULL,
 catalog_json bytea NOT NULL,
 manifest jsonb NOT NULL,
 vectors bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ai_catalog.catalog_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 active_id uuid REFERENCES ai_catalog.generations(id),
 pending_id uuid REFERENCES ai_catalog.generations(id),
 revision bigint NOT NULL DEFAULT 0,
 last_error text,
 activated_at timestamptz
);
INSERT INTO ai_catalog.catalog_state(singleton) VALUES(true);
CREATE TABLE ai_catalog.export_receipts (
 id uuid PRIMARY KEY,
 source_digest char(64) NOT NULL,
 generated_at timestamptz NOT NULL,
 product_ids bigint[] NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ai_catalog.product_tombstones (
 product_id bigint PRIMARY KEY,
 export_id uuid NOT NULL REFERENCES ai_catalog.export_receipts(id),
 confirmed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ai_catalog.generations ADD CONSTRAINT generation_export_receipt
 FOREIGN KEY(export_id) REFERENCES ai_catalog.export_receipts(id);
CREATE FUNCTION ai_catalog.immutable_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Catalog generations are immutable'; END $$;
CREATE TRIGGER immutable_generation BEFORE UPDATE OR DELETE ON ai_catalog.generations
 FOR EACH ROW EXECUTE FUNCTION ai_catalog.immutable_generation();
-- Old products/categories tables remain provenance for the 0011 migration only.
-- This view exposes the exact frozen projection used by each serving generation.
CREATE VIEW ai_catalog.generation_products AS
 SELECT g.id AS generation_id, (p->>'id')::bigint AS product_id, p AS product
 FROM ai_catalog.generations g,
 LATERAL jsonb_array_elements(convert_from(g.catalog_json,'UTF8')::jsonb->'products') p;
