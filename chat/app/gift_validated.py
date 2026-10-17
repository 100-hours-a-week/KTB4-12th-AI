"""Recover unsupported brand spellings before backend I/O, with one correction.

Suggestions are catalog data only. They never rewrite the requested filter or
execute a fuzzy match; the model must resubmit an exact valid brand itself.
"""
import difflib
from pydantic import Field
from app.gift_stable import StableV4Harness, Selection, bounded_history
from app.conversation_memory import abandoned_request
from app.gift_guided import SearchChanges, normalized
from app.gift_core.provider import ProviderError


class ValidatedHarness(StableV4Harness):
    variant = 'stable-v4.1'

    async def run(self, s, request, turn_id, emit):
        await super().run(s, request, turn_id, emit)
        status = self.metrics[turn_id].status
        if status in ('failed', 'cancelled'):
            # Web reconstructs this exact marker from durable turn status.
            # In-process training/evaluation sessions must see the same history.
            s.history = bounded_history(s.history + [{'role':'user','content':abandoned_request(request.message,status)}])

    def parse_context(self, payload, schema, s, request):
        args = SearchChanges.model_validate(payload)
        delta = args.changes.model_dump(exclude_unset=True)
        unknown = []
        for field in ('brands','exclude_brands'):
            for name in delta.get(field, []):
                resolved = self.brands.resolve(name)
                if normalized(resolved) not in self.brands.exact:
                    unknown.append(name)
        if unknown:
            # Correction suggestions do not become session state. Ambiguous or
            # nonexistent brands remain invalid after the bounded repair.
            names = self.catalog.brand_names
            self._brand_suggestions = list(dict.fromkeys(candidate for name in unknown
                for candidate in difflib.get_close_matches(name, names, n=3, cutoff=.5)))[:6]
            raise ProviderError('unknown_catalog_brand')
        return super().parse_context(payload, schema, s, request)

    def feedback(self, exc):
        if str(exc) == 'unknown_catalog_brand':
            return {'code':'unknown_catalog_brand',
                'hint':'조회는 실행되지 않았습니다. 사용자 원문과 카탈로그의 브랜드 표기를 확인해 정확히 다시 보내세요. 후보는 자동 교정이 아니며 의도와 다르면 선택하지 마세요.',
                'catalog_suggestions':getattr(self,'_brand_suggestions',[])}
        return super().feedback(exc)


class ThreeSelection(Selection):
    product_ids: list[str] = Field(max_length=3, description=Selection.model_fields['product_ids'].description)


class ThreeGiftHarness(ValidatedHarness):
    """Allow three displayed identities without changing historical protocols."""
    variant = 'stable-v4.2'
    selection_model = ThreeSelection
    selection_count_word = '세'
