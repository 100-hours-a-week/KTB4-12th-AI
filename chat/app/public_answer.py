"""Public answer inputs have a separate provenance boundary from private planning.

Recipient snapshots, planner text, selector reasons, retrieval queries and legacy
assistant messages never cross this boundary. Product fields originate in the
public search catalog; conversation constraints originate in original user text.
"""
import json,re

from app.conversation_memory import history_messages, memory_prompt, turns_through

PUBLIC_ANSWER_VERSION = 'public-answer-v1'
PRODUCT_FIELDS = ('productId', 'name', 'price', 'brand', 'description', 'category')
CARD_REASON = '공개 상품 정보와 가격을 비교할 수 있는 선물 후보입니다.'


def public_products(products):
    result = []
    for product in products:
        ident = product.get('productId')
        if type(ident) is not int or not 1 <= ident <= 2**53 - 1:
            raise ValueError('Invalid public product identity')
        facts = {'productId': ident}
        for key in PRODUCT_FIELDS[1:]:
            value = product.get(key)
            if value is None or (key == 'price' and type(value) is int) or (key != 'price' and isinstance(value, str)):
                facts[key] = value
            else:
                raise ValueError('Invalid public product fact')
        image=product.get('image')
        if isinstance(image,str) and re.fullmatch(r'/images/[a-f0-9]+-\d+\.(avif|webp)',image):facts['image']=image
        result.append(facts)
    return result


def public_cards(products):
    # Selection may use private preferences. Its generated explanation cannot.
    return [{'productId': product['productId'], 'reason': CARD_REASON} for product in products]


def previous_public_products(context):
    answer = context.get('publicAnswer', {})
    if not isinstance(answer, dict) or answer.get('version') != PUBLIC_ANSWER_VERSION:
        return []  # Legacy QA selectedProducts may carry private derivations.
    return public_products(answer.get('products', []))


def answer_messages(prompt, turns, tid, products, searched, entries=()):
    originals = {turn['turnId']: turn['message'] for turn in turns_through(turns, tid)}
    verified = [entry for entry in entries if isinstance(entry, dict)
                and isinstance(entry.get('turnId'), str)
                and entry['turnId'] in originals and entry.get('text') == originals[entry['turnId']]]
    facts = public_products(products)
    # No planner filters, clarification, selector reason, profile or tool query.
    data = {'searched': bool(searched), 'products': facts, 'cards': public_cards(facts)}
    instruction = prompt + memory_prompt(verified) + '\n짧고 자연스럽게 답한다. 제공된 공개 상품 사실과 사용자 원문으로만 설명한다. 추천할 때는 선택 상품의 가격과 공개된 특성을 설명한다. 저장 취향 자료는 이 답변 단계에 제공되지 않는다. 검색하지 않았다면 사용자 원문에 있는 모순이나 필수 호환정보만 확인하고 선택 상품이 없는데 새 상품을 만들지 않는다. 검색했지만 선택 상품이 없으면 현재 검색 자료의 한계와 다음 선택지를 설명한다.\n' + json.dumps(data, ensure_ascii=False)
    return [{'role': 'system', 'content': instruction}] + history_messages(turns, tid, include_assistant=False)
