"""Validate fixture shape before making paid model requests; never change grades."""
from app.gift_core.contracts import Filters


def validate_cases(cases):
    if not cases or len({c['id'] for c in cases})!=len(cases):
        raise ValueError('Empty or duplicate evaluation cases')
    for case in cases:
        if case.get('split')=='final':raise ValueError('Final data forbidden during harness development')
        for turn in case['turns']:
            e=turn['expect']
            if e['action']=='recommend':
                if not {'min_count','max_count','count','filters','allowed_ids'} <= e.keys():
                    raise ValueError('Incomplete state-grader fixture: '+case['id'])
                if not 0<=e['min_count']<=e['count']<=e['max_count']<=3:
                    raise ValueError('Invalid expected card count: '+case['id'])
                Filters.model_validate(e['filters'])
            elif e['action']!='text':
                raise ValueError('Unknown expected action: '+case['id'])
