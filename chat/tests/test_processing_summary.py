"""The processing panel uses recorded facts, never profile prose or explanations."""
import json

from app.turn_report import processing_summary, report


def test_processing_facts_use_the_frozen_profile_and_current_pool_only():
    private = 'SYNTHETIC_PRIVATE_CANARY'
    context = {'model': 'synthetic/model', 'snapshotId': 'catalog-12',
        'recipientProfile': {'status': 'AVAILABLE', 'inputSourceVersion': 8,
            'analysisSourceVersion': 7, 'receivedAt': '2026-10-19T12:00:00Z',
            'data': {'giftPreference': private, 'dislikedTags': [private]}},
        'observedProducts': [{'name': private}, {'name': private}],
        'diagnostics': {'modelCalls': [{'status': 'failed', 'request': private,
            'response': {'reasoning': private}}]},
        'lowLatencyState': {'lastProducts': [private], 'publicFilters': {'exclude_category_ids': ['77']}}}
    result = processing_summary(context)
    assert result['profile'] == {'status': 'AVAILABLE', 'sourceVersion': 8,
        'receivedAt': '2026-10-19T12:00:00+00:00', 'analysisSourceVersion': 7}
    assert result['retrieval'] == {'candidateCount': 2, 'exclusionReasons': [], 'catalogSnapshotId': 'catalog-12'}
    assert result['model'] == {'name': 'synthetic/model', 'status': 'failed'}
    assert private not in json.dumps(result) and '77' not in json.dumps(result)


def test_absent_or_malformed_facts_are_not_inferred_from_answer_or_filters():
    result = processing_summary({'recipientProfile': {'status': 'private text',
        'inputSourceVersion': True, 'analysisSourceVersion': -1, 'receivedAt': '2026-10-19'},
        'model': 'private text', 'snapshotId': {'private': 'text'}, 'diagnostics': [],
        'selectedProducts': [{}, {}], 'lowLatencyState': {'lastProducts': [{}, {}]}})
    assert result['profile'] == {'status': None, 'sourceVersion': None, 'receivedAt': None, 'analysisSourceVersion': None}
    assert result['retrieval']['candidateCount'] is None
    assert result['retrieval']['exclusionReasons'] == []
    assert result['model'] == {'name': None, 'status': None}
    assert processing_summary({'candidates': []})['retrieval']['candidateCount'] == 0
    assert processing_summary({'recipientProfile': {'source': 'BACKEND_REQUEST',
        'status': 'NONE', 'data': {'sourceVersion': 0}}})['profile']['sourceVersion'] == 0


def test_existing_report_adds_metadata_without_reinterpreting_final_status():
    result = report('conversation', {'turnId': 'turn', 'status': 'COMPLETED',
        'message': 'synthetic question', 'blocks': [], 'lastEventId': 1}, {})
    assert result['status'] == 'COMPLETED'
    assert result['processing']['model']['status'] is None
    assert result['processing']['retrieval']['candidateCount'] is None
