"""Prepare a separate immutable search projection; never modify the source catalog."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def compact(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    raw = (args.source / "catalog_enriched.json").read_bytes()
    original = json.loads(raw)
    products, image_sources = [], []
    for p in original["products"]:
        attrs = {str(k): compact(v) for k, v in (p.get("attributes") or {}).items() if isinstance(v, (str, int, float)) and str(v).strip()}
        identity = compact(p["name"])
        attributes = " / ".join(f"{k.removeprefix('source_')}: {v}" for k, v in attrs.items())
        category = " > ".join(filter(None, [p.get("category_group"), p.get("category")]))
        sections = [f"상품명: {identity}", f"브랜드: {p.get('brand', '')}", f"분류: {category}", f"종류: {p.get('product_kind', '')}"]
        if attributes:
            sections.append(f"속성: {attributes}")
        description = compact(p.get("description", ""))
        if description:
            sections.append(f"설명: {description}")
        document = "\n".join(sections)
        img = args.source / p["image_path"]
        image_bytes = img.read_bytes()
        image_key = hashlib.sha256(image_bytes + b"product-search-image-v1-q54-384-768").hexdigest()[:24]
        image_sources.append({"key": image_key, "path": str(img.resolve()), "original_bytes": len(image_bytes)})
        products.append({
            "id": p["product_id"], "backend_product_id": None,
            "name": p["name"], "normalized_name": p.get("normalized_name", p["name"]),
            "brand": p.get("brand", ""), "category_id": p["category_id"],
            "category": p["category"], "category_group": p["category_group"],
            "kind": p.get("product_kind", ""), "product_type": p.get("product_type"),
            "price": p["price_krw"], "description": description, "attributes": attrs,
            "tags": p.get("tags", []), "availability": "unknown",
            "product_url": p.get("product_url", ""), "image_key": image_key,
            "image": f"/images/{image_key}-384.avif", "image_large": f"/images/{image_key}-768.avif",
            "image_fallback": f"/images/{image_key}-384.webp",
            "document": document, "document_hash": hashlib.sha256(document.encode()).hexdigest(),
            "description_origin": p.get("description_origin", "unknown"),
        })
    assert len({p['id'] for p in products}) == len(products)
    content = {"format": "product-search-catalog/1", "source_sha256": hashlib.sha256(raw).hexdigest(),
               "source": "gift-catalog-20260915-v1", "availability_note": "수집 당시 상품 자료 · 현재 판매 상태 미확인",
               "taxonomy": original["category_taxonomy"], "products": products}
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    ROOT.joinpath("data").mkdir(exist_ok=True)
    ROOT.joinpath("runtime").mkdir(exist_ok=True)
    ROOT.joinpath("data/catalog.json").write_bytes(serialized)
    ROOT.joinpath("runtime/image-sources.json").write_text(json.dumps(image_sources))
    print(json.dumps({"products": len(products), "source_bytes": len(raw), "source_image_bytes": sum(x['original_bytes'] for x in image_sources), "catalog_sha256": hashlib.sha256(serialized).hexdigest()}))


if __name__ == "__main__":
    main()
