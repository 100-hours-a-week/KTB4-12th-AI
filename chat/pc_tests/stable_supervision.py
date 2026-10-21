"""Offline test helpers extracted unchanged from the captured PC training tools.

Only the pure scenario adapter/provider are retained; no training jobs or data
are imported. See PROVENANCE.json for the original files and content hashes.
"""
import copy
import json
from app.gift_core.contracts import Filters
from app.gift_core.provider import tool_events
from app.gift_protocol import DEFAULT

VERSION = DEFAULT

def matching_ids(turn,context):
 e=turn['expect'];allowed=set(e['allowed_ids'])
 return [p['product_id'] for p in context['products'] if p['product_id'] in allowed
  and (e.get('max_price') is None or p['price']<=e['max_price'])
  and (not e.get('brands') or p['brand'] in e['brands']) and p['brand'] not in e.get('exclude_brands',[])]

def sparse_target(turn, state, *, flat=False, schema=None):
    """Translate a semantic state transition, not serialized historical calls."""
    previous = Filters.model_validate(state['filters']).model_dump()
    args = turn['context_args']
    desired = Filters.model_validate(args['filters']).model_dump()
    if schema is not None:
        fields = schema['properties']
        flat = 'changes' not in fields
        fields = fields if flat else fields['changes']['properties']
        if 'recipient_scope' not in fields and desired['recipient_scope'] != 'current':
            raise ValueError('Scenario requires recipient_scope; author a new scenario for the selected harness')
        if 'delivery_method' not in fields and desired['product_type'] is not None:
            raise ValueError('Scenario requires delivery_method; author a new scenario for the selected harness')
    switching = previous['recipient_scope'] != desired['recipient_scope']
    if desired['budget_action'] == 'preserve':
        desired['min_price'] = None if switching else previous['min_price']
        desired['max_price'] = None if switching else previous['max_price']
    elif desired['budget_action'] == 'clear':
        desired['min_price'] = desired['max_price'] = None
    base = Filters().model_dump() if switching else previous
    delta = {key: value for key, value in desired.items() if key != 'budget_action' and value != base[key]}
    if switching:
        delta['recipient_scope'] = desired['recipient_scope']
    if 'product_type' in delta:
        delta['delivery_method'] = delta.pop('product_type')
    if schema is not None and delta.keys() - fields.keys():
        raise ValueError('Scenario changes filters unavailable in the selected harness')
    if flat:
        if {'min_price', 'max_price'} & delta.keys():
            delta.update(min_price=desired['min_price'], max_price=desired['max_price'])
        return {'queries': copy.deepcopy(args['queries']), **delta}
    return {'queries': copy.deepcopy(args['queries']), 'changes': delta}

class VerifiedStableProvider:
    mode = 'verified-supervision-fixture'

    def __init__(self):
        self.calls = []
        self.turn = None

    async def stream(self, messages, tools, model):
        turn = self.turn
        names = [t['function']['name'] for t in tools]
        if turn['expect']['action'] == 'text':
            if turn.get('derive_last_fact') and 'recall_gift' in names:
                name, args = 'recall_gift', {'position': turn['expect'].get('fact_index', 0) + 1}
            else:
                text = turn['target_text']
                completion = {'role': 'assistant', 'content': text}
                events = [{'kind':'text','value':text}, {'kind':'finish','value':'stop'}]
                name = None
        elif 'gift_context' in names:
            state = json.loads(messages[0]['content'].split('STATE_JSON\n', 1)[1])
            schema = next(t['function']['parameters'] for t in tools if t['function']['name'] == 'gift_context')
            name, args = 'gift_context', sparse_target(turn, state, schema=schema)
        elif 'show_gifts' in names:
            context = json.loads(messages[-1]['content'])
            ids = matching_ids(turn, context)
            preferred = turn.get('preferred_ids', [])
            ids.sort(key=lambda pid: preferred.index(pid) if pid in preferred else len(preferred))
            selected = ids[:turn['expect']['count']]
            if len(selected) != turn['expect']['count']:
                raise ValueError('infeasible_verified_target')
            name, args = 'show_gifts', {'product_ids': selected}
        else:
            raise ValueError('unexpected_training_stage')
        if name:
            completion = {'role':'assistant', 'content':None, 'tool_calls':[{'id':'verified-call','type':'function',
                'function':{'name':name, 'arguments':copy.deepcopy(args)}}]}
            events = tool_events(name, args)
        self.calls.append({'prompt':copy.deepcopy(messages), 'tools':copy.deepcopy(tools), 'completion':[completion]})
        for event in events:
            yield event
