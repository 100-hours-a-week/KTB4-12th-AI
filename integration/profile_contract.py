"""Local Console/synthetic harness input boundary; no provider or Profiler core imports."""
from pydantic import BaseModel, ConfigDict, Field


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DislikedCategoryInput(StrictInput):
    categoryId: int = Field(strict=True, ge=1, le=9_007_199_254_740_991)
    categoryName: str = Field(min_length=1, max_length=200)


class ReviewInput(StrictInput):
    productId: int = Field(strict=True, ge=1, le=9_007_199_254_740_991)
    rating: int = Field(strict=True, ge=1, le=5)
    reviewText: str | None = None


class ProfileInput(StrictInput):
    recipientUserId: int = Field(strict=True, ge=1, le=9_007_199_254_740_991)
    sourceVersion: int = Field(strict=True, ge=1, le=9_007_199_254_740_991)
    giftPreference: str | None = Field(default=None, max_length=500)
    dislikedCategories: list[DislikedCategoryInput] = Field(default_factory=list, max_length=5)
    reviews: list[ReviewInput] = Field(default_factory=list, max_length=10)
