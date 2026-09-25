from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from .identity import BIGINT_MAX

ProductId = Annotated[int, Field(strict=True, ge=1, le=BIGINT_MAX, json_schema_extra={'format': 'int64'})]
CategoryId = Annotated[int, Field(strict=True, ge=1, le=BIGINT_MAX, json_schema_extra={'format': 'int64'})]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel, str_strip_whitespace=True)


class Filters(Model):
    min_price: int | None = Field(default=None, ge=0, strict=True)
    max_price: int | None = Field(default=None, ge=0, strict=True)
    category_ids: list[CategoryId] = Field(default_factory=list, max_length=100)
    exclude_category_ids: list[CategoryId] = Field(default_factory=list, max_length=100)
    brands: list[str] = Field(default_factory=list, max_length=50)
    exclude_brands: list[str] = Field(default_factory=list, max_length=50)
    product_types: list[Literal["Shipping", "Pickup", "Voucher"]] = Field(default_factory=list)
    exclude_product_ids: list[ProductId] = Field(default_factory=list, max_length=500)
    availability: Literal["any", "available", "unavailable", "unknown", "available_or_unknown"] = "any"

    @model_validator(mode="after")
    def ordered_price(self):
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("최소 가격은 최대 가격보다 클 수 없습니다.")
        return self


class Preferences(Model):
    preferred_category_ids: list[CategoryId] = Field(default_factory=list, max_length=100)
    downrank_category_ids: list[CategoryId] = Field(default_factory=list, max_length=100)


class SearchRequest(Model):
    query: str = Field(default="", max_length=500)
    filters: Filters = Field(default_factory=Filters)
    preferences: Preferences = Field(default_factory=Preferences)
    mode: Literal["hybrid", "lexical", "dense"] = "hybrid"
    limit: int = Field(default=30, ge=1, le=100, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)


class ProductRequest(Model):
    ids: list[ProductId] = Field(min_length=1, max_length=100)
    snapshot_id: str | None = None


class ServiceSearchRequest(SearchRequest):
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=80)


class InvalidSearchFilter(ValueError):
    """A well-formed request with an unknown catalog filter value."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SnapshotMismatch(ValueError):
    """The caller's catalog version is no longer active."""


class FeedbackRequest(Model):
    search_id: str = Field(min_length=1, max_length=80)
    product_id: ProductId | None = None
    verdict: Literal["relevant", "irrelevant", "filter_violation", "missing"]
    note: str = Field(default="", max_length=1200)
