CREATE SCHEMA IF NOT EXISTS ai_search;

CREATE TABLE IF NOT EXISTS ai_search.products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category_id TEXT NOT NULL,
    price_krw INTEGER NOT NULL CHECK (price_krw > 0),
    description TEXT NOT NULL
);
