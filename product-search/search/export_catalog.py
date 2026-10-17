"""Validate Backend's export contract and build an isolated search projection."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

WireId = Annotated[int, Field(strict=True,ge=1,le=2**53-1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ExportProduct(Contract):
    productId: WireId
    name: StrictStr
    brand: StrictStr
    description: StrictStr | None
    categoryId: WireId
    categoryName: StrictStr
    price: Annotated[int, Field(strict=True,ge=0)]
    available: StrictBool
    updatedAt: datetime

    @field_validator('updatedAt', mode='before')
    @classmethod
    def utc_time(cls, value):
        return utc(value)


class ExportData(Contract):
    generatedAt: datetime
    products: list[ExportProduct]

    @field_validator('generatedAt', mode='before')
    @classmethod
    def utc_time(cls, value):
        return utc(value)


class ExportResponse(Contract):
    message: StrictStr
    data: ExportData


class ChildCategory(Contract):
    categoryId: WireId
    name: StrictStr


class GroupCategory(ChildCategory):
    children: list[ChildCategory]


class CategoryData(Contract):
    categories: list[GroupCategory]


class CategoryResponse(Contract):
    message: StrictStr
    data: CategoryData


def utc(value):
    if not isinstance(value, str):
        raise ValueError('Expected UTC ISO8601 string')
    date = datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
        raise ValueError('Expected UTC ISO8601 string')
    return date


def encode(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


def document(p):
    return '\n'.join([f"상품명: {p.name}", f"브랜드: {p.brand}", f"분류: {p.categoryName}", f"설명: {p.description or ''}"])


def project(export_json, categories_json, old_catalog, product_map, category_map):
    export = ExportResponse.model_validate(export_json).data
    categories = CategoryResponse.model_validate(categories_json).data.categories
    ids = [p.productId for p in export.products]
    if ids != sorted(set(ids)):
        raise ValueError('Export product IDs must be unique and ascending')
    if any(p.updatedAt > export.generatedAt for p in export.products):
        raise ValueError('Product updatedAt exceeds export generatedAt')
    previous = old_catalog.get('export_generated_at')
    if previous and export.generatedAt < utc(previous):
        raise ValueError('Export is older than the active catalog')
    sources = {v:k for k,v in product_map.items()}
    category_sources = {v:k for k,v in category_map.items()}
    taxonomy, leaves, seen = [], {}, set()
    for group in categories:
        if group.categoryId in seen: raise ValueError('Duplicate category ID')
        seen.add(group.categoryId)
        taxonomy.append({'category_id':group.categoryId,'source_category_id':category_sources.get(group.categoryId),
            'parent_id':None,'category':group.name,'category_group':group.name})
        for leaf in group.children:
            if leaf.categoryId in seen: raise ValueError('Duplicate category ID')
            seen.add(leaf.categoryId)
            row = {'category_id':leaf.categoryId,'source_category_id':category_sources.get(leaf.categoryId),
                'parent_id':group.categoryId,'category':leaf.name,'category_group':group.name}
            taxonomy.append(row)
            leaves[leaf.categoryId] = row
    old_products = {p['id']:p for p in old_catalog['products']}
    products = []
    for item in export.products:
        leaf = leaves.get(item.categoryId)
        if not leaf or item.categoryName != leaf['category']:
            raise ValueError('Export category missing or name differs from hierarchy')
        old = old_products.get(item.productId, {})
        source = sources.get(item.productId)
        if old.get('source_product_id') is not None and source != old['source_product_id']:
            raise ValueError('Product mapping conflicts with the active catalog')
        text = document(item)
        # Images/type are source enrichment, not supplied by this export version.
        p = {'id':item.productId,'source_product_id':source,'name':item.name,'normalized_name':item.name,
            'brand':item.brand,'category_id':item.categoryId,'source_category_id':leaf['source_category_id'],
            'parent_category_id':leaf['parent_id'],'category':leaf['category'],'category_group':leaf['category_group'],
            'kind':'','product_type':old.get('product_type'),'price':item.price,'description':item.description or '',
            'attributes':{},'tags':[],'availability':'available' if item.available else 'unavailable',
            'product_url':old.get('product_url',''),'image':old.get('image',''),'image_large':old.get('image_large',''),
            'image_fallback':old.get('image_fallback',''),'document':text,
            'document_hash':hashlib.sha256(text.encode()).hexdigest(),'description_origin':'backend-export',
            'source_updated_at':item.updatedAt.isoformat()}
        products.append(p)
    return {'format':'product-search-catalog/3','document_contract':'backend-export/1',
        'export_generated_at':export.generatedAt.isoformat(),'source':'backend-export',
        'id_mapping_provenance':old_catalog.get('id_mapping_provenance',{}),
        'availability_note':'Backend export 시점 판매 상태 · 화면 표시는 Backend에서 다시 조회',
        'taxonomy':{'categories':taxonomy},'products':products}
