from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.limits import MAX_CARD_PRODUCTS, MAX_CARD_REASON_LENGTH, MAX_MESSAGE_LENGTH

SafeIntegerId = Annotated[int, Field(strict=True, ge=1, le=9_007_199_254_740_991)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel,
        validate_by_alias=True, validate_by_name=False, serialize_by_alias=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def reject_null_characters(cls, value):
        # PostgreSQL text/JSONB cannot store NUL; reject it before any DB call.
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL characters are not supported")
        return value


class PutConversation(StrictModel):
    sender_id: SafeIntegerId = Field(alias="senderUserId")
    recipient_id: SafeIntegerId = Field(alias="recipientUserId")


class DislikedCategory(StrictModel):
    category_id: SafeIntegerId
    category_name: Annotated[str, Field(min_length=1, max_length=200)]


class RecipientContext(StrictModel):
    recipient_user_id: SafeIntegerId
    status: Literal['AVAILABLE', 'NONE', 'UNAVAILABLE']
    source_version: Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)] | None
    gift_preference: Annotated[str, Field(max_length=500)] | None
    disliked_categories: list[DislikedCategory] = Field(max_length=5)

    @model_validator(mode='after')
    def consistent(self):
        populated = bool((self.gift_preference or '').strip() or self.disliked_categories)
        if self.status == 'AVAILABLE' and (self.source_version is None or not populated):
            raise ValueError('AVAILABLE requires a version and preferences')
        if self.status != 'AVAILABLE' and (self.gift_preference is not None or self.disliked_categories):
            raise ValueError('NONE/UNAVAILABLE must not contain preferences')
        if (self.status == 'UNAVAILABLE') != (self.source_version is None):
            raise ValueError('Only UNAVAILABLE uses a null version')
        ids = [category.category_id for category in self.disliked_categories]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate disliked category')
        self.disliked_categories.sort(key=lambda category: category.category_id)
        return self


class PreparedContext(StrictModel):
    product_ids: list[SafeIntegerId] = Field(min_length=1,max_length=30)
    snapshot_id: str = Field(min_length=1,max_length=80)
    @model_validator(mode='after')
    def unique(self):
        if len(set(self.product_ids))!=len(self.product_ids):raise ValueError('Duplicate prepared product ID')
        return self


class PutTurn(StrictModel):
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)]
    # Older servers forbid unknown body fields, so they reject this diagnostic
    # request instead of silently treating its header as an ordinary chat.
    quick_check: bool = Field(default=False, alias='quickCheck', strict=True)
    model: str | None = Field(default=None, max_length=160, description='OpenRouter model ID. Omit to use the server default; preserve this value on retries.')
    prepared_context: PreparedContext | None = Field(default=None,description='Verified candidate IDs/snapshot; immutable on retry. Prepared-pool recommendation uses one responder call.')
    recipient_context: RecipientContext | None = Field(default=None, description='Backend-owned source snapshot for this turn. Omission means not supplied, never reuse a previous snapshot.')

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model_id(cls, value):
        if value is None: return None
        if not isinstance(value, str):
            # Pydantic wraps ValueError as request validation; TypeError escapes it.
            raise ValueError('model must be a string')  # noqa: TRY004
        from app.model_config import normalize_model
        return normalize_model(value)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value):
        return value.strip() if isinstance(value, str) else value


class ProductReference(StrictModel):
    product_id: SafeIntegerId
    reason: Annotated[str, Field(min_length=1, max_length=MAX_CARD_REASON_LENGTH)]

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class TextBlock(StrictModel):
    type: Literal["text"]
    text: str


class CardsBlock(StrictModel):
    type: Literal["cards"]
    products: Annotated[list[ProductReference], Field(min_length=1, max_length=MAX_CARD_PRODUCTS)]

    @model_validator(mode="after")
    def unique_products(self) -> Self:
        ids = [product.product_id for product in self.products]
        if len(ids) != len(set(ids)):
            raise ValueError("CardsBlock productId values must be unique")
        return self


# Internal PC harness renderer DTO. The public AI-BE Block union stays unchanged.
class CatalogReference(ProductReference):
    reason: Annotated[str, Field(min_length=1, max_length=700)]

class CatalogBlock(CardsBlock):
    type: Literal['catalog']
    products: Annotated[list[CatalogReference], Field(min_length=1, max_length=30)]


Block = TextBlock | CardsBlock


class TurnError(StrictModel):
    message: str


class Turn(StrictModel):
    turn_id: UUID4
    status: Literal["PROCESSING", "COMPLETED", "CANCELLED", "FAILED"]
    message: str
    progress_message: str | None = Field(default=None, exclude_if=lambda value: value is None)
    blocks: list[Block]
    last_event_id: Annotated[int, Field(ge=0)]
    error: TurnError | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status == "FAILED" and self.error is None:
            raise ValueError("FAILED Turn requires error")
        if self.status != "FAILED" and self.error is not None:
            raise ValueError("Only FAILED Turn may contain error")
        if self.status != "PROCESSING" and self.progress_message is not None:
            raise ValueError("Terminal Turn cannot contain progressMessage")
        if sum(block.type == "cards" for block in self.blocks) > 1:
            raise ValueError("Turn may contain at most one cards block")
        return self


class Conversation(StrictModel):
    conversation_id: UUID4
    turns: list[Turn]


Payload = TypeVar("Payload")


class SuccessResponse(StrictModel, Generic[Payload]):
    message: str
    data: Payload


class HTTPErrorDetail(StrictModel):
    code: str
    trace_id: str


class ErrorResponse(StrictModel):
    message: str
    error: HTTPErrorDetail
