"""Public card preparation and read-only recovery of invalid legacy displays."""

import copy

from pydantic import ValidationError

from app.errors import APIError
from app.limits import MAX_CARD_REASON_LENGTH
from app.models import CardsBlock, Turn

UNREADABLE_CARDS = "\n\n저장된 상품 카드가 표시 형식과 맞지 않아 표시하지 못했어요. 원본 기록은 변경하지 않았습니다.\n\n"


def catalog_reason(product):
    """Select a whole source fact; never cut an option or qualification in half."""
    for field, prefix in (
        ("description", "상품 정보: "),
        ("name", "상품 정보: "),
        ("brand", "브랜드: "),
    ):
        value = product.get(field)
        if isinstance(value, str) and value.strip():
            quote = value.strip()
            if len(prefix + quote) <= MAX_CARD_REASON_LENGTH:
                return prefix + quote, {
                    "field": field,
                    "quote": quote,
                    "origin": "catalog",
                    "serverRendered": True,
                }
    price = product.get("price")
    if type(price) is int and price >= 0:
        reason = f"상품 가격: {price:,}원"
        if len(reason) <= MAX_CARD_REASON_LENGTH:
            return reason, {
                "field": "price",
                "value": price,
                "quote": f"{price:,}원",
                "origin": "catalog",
                "serverRendered": True,
            }
    raise ValueError("no_complete_public_card_fact")


def cards_payload(payload):
    """Validate the public card envelope before persistence or event publication."""
    block = CardsBlock.model_validate({"type": "cards", **payload})
    return {
        "products": [product.model_dump(by_alias=True) for product in block.products]
    }


def harness_cards(products):
    """Map the harness result to AI-BE fields without changing count or order."""
    return cards_payload(
        {
            "products": [
                {"productId": int(product["product_id"]), "reason": product["reason"]}
                for product in products
            ]
        }
    )


def readable_turn(state):
    """Project invalid stored cards as an explicit notice, without repairing DB data."""
    public_state = copy.deepcopy(state)
    blocks = []
    has_invalid_cards = False
    for block in public_state["blocks"]:
        if block.get("type") == "cards":
            try:
                CardsBlock.model_validate(block)
            except ValidationError:
                block = {"type": "text", "text": UNREADABLE_CARDS}
                has_invalid_cards = True
        blocks.append(block)
    if has_invalid_cards:
        # The SSE equivalent is an answer.delta, so preserve the same text merge.
        merged_blocks = []
        for block in blocks:
            if merged_blocks and merged_blocks[-1]["type"] == block["type"] == "text":
                merged_blocks[-1]["text"] += block["text"]
            else:
                merged_blocks.append(block)
        blocks = merged_blocks
    public_state["blocks"] = blocks
    try:
        return Turn.model_validate(public_state).model_dump(mode="json", by_alias=True)
    except ValidationError as exc:
        raise APIError(503, "저장된 질문 상태를 안전하게 표시할 수 없습니다.") from exc


def readable_conversation(value):
    return {**value, "turns": [readable_turn(turn) for turn in value["turns"]]}


def readable_replay(batch, state):
    """Use the same read projection for SSE while retaining stored event IDs."""
    events = copy.deepcopy(batch)
    for event in events:
        if event["event"] == "cards":
            try:
                cards_payload(event["data"])
            except ValidationError:
                event.update(event="answer.delta", data={"delta": UNREADABLE_CARDS})
        elif event["event"] == "turn.finished":
            event["data"] = readable_turn(event["data"])
    return events, readable_turn(state)
