"""Prevent silent mixing of historical and versioned tool supervision."""
import json
import os


def models_for_version(version):
    from app.gift_protocol import harness_class
    from app.gift_guided import SearchChanges, Recall
    cls = harness_class(version)
    models = {'gift_context': getattr(cls, 'context_model', SearchChanges), 'show_gifts': cls.selection_model, 'recall_gift': Recall}
    if hasattr(cls, 'listing_model'):
        models['list_gifts'] = cls.listing_model
    if hasattr(cls, 'inspection_model'):
        models['inspect_search'] = cls.inspection_model
    return models


def validate_records(rows, frozen=None):
    from app.gift_protocol import manifest, harness_class
    from supervision_quality import identity_errors
    versions = {row.get('harnessVersion') for row in rows}
    if versions == {None}:
        if (frozen and frozen.get('harness')) or manifest()['version'] != 'baseline-v1':
            raise ValueError('Legacy targets require explicit baseline; regenerate for the selected harness')
        return {'version':'historical-baseline','records':len(rows)}
    if not rows or None in versions or len(versions) != 1:
        raise ValueError('Mixed or empty training protocol')
    version = next(iter(versions))
    protocol = manifest(version)
    if frozen and frozen.get('harness') != protocol:
        raise ValueError('Frozen training harness no longer matches execution')
    if version not in ('stable-v3','stable-v3.1','stable-v4','stable-v4.1','stable-v4.2','stable-v4.3','stable-v4.4','stable-v4.5','stable-v4.6','stable-v4.7','stable-v4.8','stable-v4.9','stable-v4.10','stable-v4.11','stable-v4.12'):
        raise ValueError('Versioned training is not registered for this protocol')
    models = models_for_version(version)
    for row in rows:
        if row.get('harnessSha256') != protocol['sourceSha256']:
            raise ValueError('Training target harness source mismatch')
        if row.get('serverRenderedTextTarget') is not False:
            raise ValueError('Server-rendered output must not become a model target')
        if len(row.get('completion',[])) != 1 or row['completion'][0]['role'] != 'assistant':
            raise ValueError('Only one actual model decision can be supervised')
        completion = row['completion'][0]
        # Unfrozen schema fixtures may omit context. Actual frozen training
        # and every context-bearing row must provide the complete decision.
        has_context=frozen is not None or 'prompt' in row or 'tools' in row
        if has_context and (not row.get('prompt') or not isinstance(row.get('tools'),list)):
            raise ValueError('Complete training prompt and tools are required')
        available={tool['function']['name'] for tool in row.get('tools',[])}
        if completion.get('tool_calls') and completion.get('content'):
            raise ValueError('Mixed text and tool-call training target')
        if has_context and identity_errors(row):
            raise ValueError('Training product identity references do not resolve')
        for call in completion.get('tool_calls',[]):
            fn=call['function'];args=fn['arguments']
            if isinstance(args,str):args=json.loads(args)
            if fn['name'] not in models:raise ValueError('Unregistered training tool')
            if has_context and fn['name'] not in available:raise ValueError('Training tool unavailable at this decision')
            models[fn['name']].model_validate(args)
    return protocol
