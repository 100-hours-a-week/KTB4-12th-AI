"""api/schemas DTO 경계 — 7.9 상품 레코드 규칙이 Pydantic으로 지켜지는지.

7.6 · 7.7(HTTP) 규격 시험은 11-08 RabbitMQ 로 대체하며 지웠다 — 요청 · 결과 메시지 규칙은 test_mq_messages · test_mq_results."""

import pytest
from pydantic import ValidationError

from profiling.schemas import ProductRecord


def test_product_record_ignores_unknown_and_accepts_view_aliases() -> None:
    base = {"productId": 1, "name": "n", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 0, "available": True, "updatedAt": "2026-09-01T00:00:00Z"}
    assert ProductRecord(**{**base, "sales": 3, "imageUrl": "x"}).productId == 1          # 모르는 필드 무시
    assert ProductRecord(**{**base, "views": 42}).viewCount == 42                         # Backend 열 이름
    assert ProductRecord(**{**base, "viewCount": 7}).viewCount == 7
    assert ProductRecord(**base).viewCount == 0                                           # 없으면 0
    with pytest.raises(ValidationError):                                                  # 필수 누락은 여전히 오류
        ProductRecord(**{k: v for k, v in base.items() if k != "price"})


def test_product_record_availability_three_values() -> None:
    """7.9는 boolean available로 보내고 우리는 3값으로 받는다. 없거나 null이면 unknown — 추정하지 않는다."""
    base = {"productId": 1, "name": "n", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 0, "updatedAt": "2026-09-01T00:00:00Z"}
    assert ProductRecord(**{**base, "available": True}).availability == "available"
    assert ProductRecord(**{**base, "available": False}).availability == "unavailable"
    assert ProductRecord(**base).availability == "unknown"                                # 필드 없음
    assert ProductRecord(**{**base, "available": None}).availability == "unknown"         # 명시적 null
    assert ProductRecord(**{**base, "availability": "unknown"}).availability == "unknown"  # 3값을 그대로 줘도 받는다
    with pytest.raises(ValidationError):
        ProductRecord(**{**base, "availability": "품절"})                                  # 3값 밖은 거부


def test_unknown_observations_require_explicit_search_source_context() -> None:
    product = {"productId":1,"name":"synthetic","brand":"b","description":None,"categoryId":1,
               "categoryName":"c","price":0,"availability":"unknown","updatedAt":None,"viewCount":None}
    with pytest.raises(ValidationError):
        ProductRecord.model_validate(product)
    export = ProductRecord.model_validate(product, context={"catalog_source":"backend-export"})
    assert export.updatedAt is None and export.viewCount is None
    record = ProductRecord.model_validate(product, context={"catalog_source":"catalog-snapshot"})
    assert record.updatedAt is None and record.viewCount is None
    for change in ({"viewCount":0},{"updatedAt":"2026-10-18T00:00:00Z"},{"availability":"available"}):
        with pytest.raises(ValidationError):
            ProductRecord.model_validate(product | change, context={"catalog_source":"catalog-snapshot"})
    for missing in ("viewCount", "updatedAt"):
        for source in ("catalog-snapshot", "backend-export"):
            with pytest.raises(ValidationError):
                ProductRecord.model_validate({k:v for k,v in product.items() if k != missing},
                                             context={"catalog_source":source})
