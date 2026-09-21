import argparse
import json
import os
from pathlib import Path

import psycopg


def load_catalog(path):
    with path.open(encoding="utf-8") as file:
        products = json.load(file)["products"]

    rows = [
        (
            product["product_id"],
            product["name"],
            product["brand"],
            product["category_id"],
            product["price_krw"],
            product["description"],
        )
        for product in products
    ]

    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(schema)
            cursor.executemany(
                """
                INSERT INTO ai_search.products
                    (product_id, name, brand, category_id, price_krw, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (product_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    brand = EXCLUDED.brand,
                    category_id = EXCLUDED.category_id,
                    price_krw = EXCLUDED.price_krw,
                    description = EXCLUDED.description
                """,
                rows,
            )

    print(f"상품 {len(rows)}건을 저장했습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="상품 JSON을 PostgreSQL에 저장합니다.")
    parser.add_argument("path", type=Path, help="정제한 products.json 경로")
    args = parser.parse_args()
    load_catalog(args.path)
