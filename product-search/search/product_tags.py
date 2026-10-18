"""Search owns stored product tags; Emmet's pure vocabulary defines their meaning."""
import hashlib
from types import SimpleNamespace

from profiling import vocab
from .export_catalog import encode


def enrich(catalog):
    brands = sorted({p["brand"] for p in catalog["products"]})
    vocab.set_catalog_brands(brands)
    for product in catalog["products"]:
        record = SimpleNamespace(brand=product["brand"], name=product["name"],
            productKind=product["kind"], categoryName=product["category"],
            parentCategoryName=product["category_group"])
        product["product_tags"] = [t for t in vocab.product_tags(record) if t["tag"].strip()]
        # Keep existing lexical weighting/document semantics. Structured product
        # tags support Profiler exclusions; they are not new BM25 query terms.
    catalog["product_tags_rule"] = vocab.RULE
    catalog["product_tags_sha256"] = vocab.tags_sha256()
    catalog["product_tags_catalog_sha256"] = hashlib.sha256(encode(brands)).hexdigest()


def validate(catalog):
    if catalog.get("product_tags_rule") != vocab.RULE or catalog.get("product_tags_sha256") != vocab.tags_sha256():
        raise ValueError("PRODUCT_TAG_RULE_MISMATCH")
    brands = sorted({p["brand"] for p in catalog["products"]})
    if catalog.get("product_tags_catalog_sha256") != hashlib.sha256(encode(brands)).hexdigest():
        raise ValueError("PRODUCT_TAG_CATALOG_MISMATCH")
    vocab.set_catalog_brands(brands)
    for product in catalog["products"]:
        tags = product.get("product_tags")
        if not isinstance(tags, list):
            raise ValueError("PRODUCT_TAGS_MISSING")
        for tag in tags:
            if (not isinstance(tag, dict) or set(tag) != {"axis", "tag", "via"}
                    or tag["axis"] not in vocab.AXES or tag["via"] not in {"field", "name"}
                    or not isinstance(tag["tag"], str) or not tag["tag"].strip()):
                raise ValueError("INVALID_PRODUCT_TAG")
        record = SimpleNamespace(brand=product["brand"], name=product["name"],
            productKind=product["kind"], categoryName=product["category"], parentCategoryName=product["category_group"])
        expected = [t for t in vocab.product_tags(record) if t["tag"].strip()]
        if tags != expected:
            raise ValueError("PRODUCT_TAG_CONTENT_MISMATCH")
