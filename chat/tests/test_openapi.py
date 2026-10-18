import os
import uuid
os.environ.setdefault('AI_SERVICE_TOKEN','test-service-token')
os.environ.setdefault('SESSION_SECRET','local-test-session-secret')
os.environ.setdefault('QA_ADMIN_PASSWORD','local-test-admin')
os.environ.setdefault('DEMO_ACCESS_TOKEN','test-team-token')
import httpx
from app import main


def test_schema_matches_validation_errors_and_sse_wire_type():
    schema=main.app.openapi()
    operations=[operation for path,methods in schema['paths'].items()
                if path.startswith(main.BASE) for operation in methods.values()]
    assert len(operations)==7
    for operation in operations:
        assert '422' not in operation['responses']
        for code in ['400','401','409','413','429','503']:
            assert operation['responses'][code]['content']['application/json']['schema']['$ref'].endswith('/ErrorResponse')
    stream=schema['paths'][main.BASE+'/{conversationId}/turns/{turnId}/events']['get']
    assert set(stream['responses']['200']['content'])=={'text/event-stream'}
    assert set(schema['components']['schemas']['HTTPErrorDetail']['properties'])=={'code','traceId'}


async def test_validation_and_oversized_request_share_documented_error_envelope():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://test') as client:
        path=main.BASE+'/'+str(uuid.uuid4());headers={'Authorization':'Bearer '+main.TOKEN}
        response=await client.put(path,json={'senderUserId':'7','recipientUserId':42},headers=headers)
        assert response.status_code==400 and response.json()['error']['code']=='INVALID_REQUEST'
        response=await client.put(path,content=b'x'*20001,headers=headers)
        assert response.status_code==413 and response.json()['error']['code']=='PAYLOAD_TOO_LARGE'
        assert uuid.UUID(response.json()['error']['traceId']).version==4
        assert response.headers['cache-control']=='no-store, no-transform'
        assert 'x-app-revision' in response.headers
