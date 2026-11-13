import copy,json,os
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import uuid4
import httpx,pytest
from app.request_audit import capture_request,trace,conversation_projection,usage,text
from app.llm_connection import ACTIVE_CONNECTION,AdmittedConnection
from app import main,security

NOW=datetime.now(timezone.utc).isoformat()
def state():return {'turnId':str(uuid4()),'status':'COMPLETED','message':'합성 공개 질문','blocks':[{'type':'cards','products':[{'productId':101,'reason':'공개 상품의 용도와 맞습니다.'}]}],'lastEventId':3}
def context():return {'sourceScope':'synthetic-local','environment':'local','revision':'e7383488','modelRequests':[{}],'requestAudit':[{'index':1,'startedAt':NOW,'offsetMs':1,'status':'completed','input':{'capture':'actual-send-time-redacted'},'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':.001,'prompt_tokens_details':{'cached_tokens':80,'cache_write_tokens':10},'completion_tokens_details':{'reasoning_tokens':5}},'toolNames':['show_gifts']}],'usage':[],'diagnostics':{'operations':[{'name':'catalog.search','startedAt':NOW,'turnOffsetMs':2,'status':'completed','arguments':{'query':'PRIVATE_QUERY'},'result':{'products':[{'productId':101}]}}]}}
def test_capture_retains_preferences_and_tool_inputs_but_not_credentials():
 raw={'model':'synthetic','messages':[{'role':'system','content':'Policy\nSTATE_JSON\n'+json.dumps({'filters':{},'recipient_preferences':{'giftPreference':'PRIVATE_CANARY'},'cached_recipient':{'preferences':'ANOTHER_PRIVATE'},'unknown':{'secret':'PRIVATE_THIRD'}})},{'role':'assistant','tool_calls':[{'function':{'name':'gift_context','arguments':'PRIVATE_QUERY'}}]},{'role':'tool','content':json.dumps({'products':[{'product_id':'101','description':'公开 catalog'}],'queries':['PRIVATE_QUERY']})}],'tools':[]}
 recorded=capture_request(raw);value=json.dumps(recorded)
 assert all(v in value for v in ('PRIVATE_CANARY','ANOTHER_PRIVATE','PRIVATE_QUERY'))
 assert 'PRIVATE_THIRD' not in value
 assert '公开 catalog' in json.dumps(recorded,ensure_ascii=False)
def test_bound_capture_and_literal_text_no_html_execution():
 r=capture_request({'messages':[{'role':'user','content':'x'*30000} for _ in range(32)]})
 assert r['truncated'] and r['originalChars']>262144
 assert text('<img src=x onerror=bad()>')=='<img src=x onerror=bad()>'
def test_totals_never_double_add_cache_or_reasoning():
 t=trace('cid',state(),context());assert t['totals']['inputTokens']==100 and t['totals']['outputTokens']==20
 u=t['modelCalls'][0]['usage'];assert u['cachedReadTokens']==80 and u['reasoningTokens']==5
 assert 'PRIVATE_QUERY' not in json.dumps(t)
@pytest.mark.parametrize('field',['prompt_tokens_details','completion_tokens_details'])
def test_malformed_usage_details_are_unknown_not_view_failure(field):
 c=context();c['requestAudit'][0]['usage'][field]=['invalid'];assert trace('cid',state(),c)['totals']['inputTokens']==100
 assert usage({'prompt_tokens_details':['invalid']})['cachedReadTokens'] is None
def test_legacy_and_missing_receipt_not_fabricated():
 t=trace('cid',state(),{});assert t['recording']['calls']=='not-recorded-legacy' and t['modelCalls']==[] and t['totals']['costUsd'] is None
 c=context();c['modelRequests'].append({});assert trace('cid',state(),c)['totals']['inputTokens'] is None
 c['requestAudit'][0].pop('usage');assert trace('cid',state(),c)['totals']['costUsd'] is None

def test_explicit_zero_attempts_distinguish_pre_model_failure_from_missing_records():
 c={'agent':'gift-tool-loop-v1','modelAttempts':0,'modelRequests':[],
    'usage':[],'outcome':'FAILED','failureStage':'context','failure':'APIError'}
 t=trace('cid',{**state(),'status':'FAILED'},c)
 assert t['recording']['calls']=='not-started'
 assert t['totals']['attemptsObserved']==0 and t['modelCalls']==[]
 assert t['totals']['inputTokens'] is None and t['totals']['costUsd'] is None
 assert t['diagnostic']['stage']=='context' and t['diagnostic']['code']=='APIError'

@pytest.mark.parametrize('changes',[
 {'modelAttempts':None},{'modelAttempts':False},{'modelAttempts':1},
 {'modelRequests':None},{'modelRequests':[{}]},{'agent':'unknown'},
 {'usage':[{'prompt_tokens':1,'completion_tokens':1}]},
 {'diagnostics':{'modelCalls':[{'status':'failed'}]}},
])
def test_incomplete_or_conflicting_evidence_never_claims_model_not_started(changes):
 c={'agent':'gift-tool-loop-v1','modelAttempts':0,'modelRequests':[],**changes}
 t=trace('cid',state(),c)
 assert t['recording']['calls']=='not-recorded-legacy'
 assert t['totals']['attemptsObserved'] is None
def test_details_expire_without_claiming_persisted_data_deletion():
 old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();t=trace('cid',state(),context(),details=True,created=old)
 assert 'publicInput' not in t and 'input' not in t['modelCalls'][0]
 assert t['recording']['storedRecordRetention']=='existing Chat store policy; no new deletion job'
def test_default_projection_masks_prose_and_retains_pagination():
 t=state();data={'conversationId':'cid','turns':[{'turnId':t['turnId'],'state':t,'context':context(),'created':NOW,'feedback':[],'reviews':[]}],'nextCursor':20}
 out=conversation_projection(data);assert out['nextCursor']==20 and out['turns'][0]['state']['blocks']==[]
 assert '합성 공개 질문' not in json.dumps(out,ensure_ascii=False);assert data['turns'][0]['state']['message']=='합성 공개 질문'
async def test_arbitrarily_named_registered_secret_is_redacted():
 client=httpx.AsyncClient(headers={'Authorization':'Bearer private-credential-with-neutral-env-name'})
 conn=AdmittedConnection('openai-compatible','http://127.0.0.1/v1','mock',1,client)
 token=ACTIVE_CONNECTION.set(conn)
 try:assert text('echo private-credential-with-neutral-env-name')=='echo [REDACTED]'
 finally:ACTIVE_CONNECTION.reset(token);await client.aclose()
async def test_trace_route_requires_admin_and_exact_binding(monkeypatch):
 tid=str(uuid4());cid=str(uuid4());turn={**state(),'turnId':tid}
 class Store:
  async def call(self,method,**args):
   assert method=='admin_conversation' and args['cid']==cid and args['focus_tid']==tid
   return {'conversationId':cid,'turns':[{'turnId':tid,'state':turn,'context':context(),'created':NOW,'reviews':[{'note':'다시 읽을 합성 메모'}],'feedback':[{'payload':{'note':'합성 피드백'}}]}]}
 monkeypatch.setattr(main.app.state,'store',Store(),raising=False);security.rate.clear()
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  path=f'/admin/api/conversations/{cid}/turns/{tid}/trace?details=true'
  assert (await client.get(path)).status_code==401
  assert (await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})).status_code==200
  response=await client.get(path);assert response.status_code==200 and response.json()['publicInput']=='합성 공개 질문'
  assert response.headers['cache-control']=='no-store, no-transform'
  assert response.json()['reviewNotes']==[{'note':'다시 읽을 합성 메모'}]
  assert response.json()['feedbackNotes'][0]['note']=='합성 피드백'
  assert (await client.get(path,headers={'Origin':'https://evil.invalid'})).status_code==403

@pytest.mark.parametrize('receipt',[
 {'prompt_tokens':100,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':0,'completion_tokens':0,'cost':0},
 {'prompt_tokens':100,'completion_tokens':20},
 {'prompt_tokens':True,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':-1,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':100,'completion_tokens':None,'cost':.001},
 {'prompt_tokens':100,'completion_tokens':20,'cost':float('nan')},
 None,
])
@pytest.mark.parametrize('expected',[1,2])
def test_list_and_detail_share_complete_receipt_totals(receipt,expected):
 from app.request_audit import list_summary
 ctx=context();ctx['modelRequests']=[{}]*expected;ctx['requestAudit'][0]['usage']=receipt
 detailed=trace('cid',state(),ctx)['totals']
 summary=list_summary({'usage':[receipt],'attemptCount':expected})
 for key in ('inputTokens','outputTokens','costUsd','usageState'):
  assert summary[key]==detailed[key]

def test_list_requires_attempt_count_but_legacy_detail_keeps_observed_receipts():
 from app.request_audit import list_summary
 ctx=context();ctx.pop('modelRequests')
 assert trace('cid',state(),ctx)['totals']['inputTokens']==100
 assert list_summary({'usage':[ctx['requestAudit'][0]['usage']]})['inputTokens'] is None


def test_notes_require_explicit_unexpired_detail_and_mask_secrets():
 notes=[{'note':'검토 메모 token=synthetic-secret'}];feedback=[{'payload':{'verdict':'helpful','note':'합성 의견'}}]
 assert 'reviewNotes' not in trace('cid',state(),context(),reviews=notes,feedback=feedback)
 out=trace('cid',state(),context(),details=True,created=NOW,reviews=notes,feedback=feedback)
 assert out['reviewNotes']==[{'note':'검토 메모 [redacted]'}]
 assert out['feedbackNotes']==[{'verdict':'helpful','note':'합성 의견'}]
 old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
 assert 'reviewNotes' not in trace('cid',state(),context(),details=True,created=old,reviews=notes)


def test_registered_neutral_credential_is_masked_without_active_connection(monkeypatch):
 monkeypatch.setenv('CHAT_LLM_SECRET_REFS','NEUTRAL_CREDENTIAL')
 monkeypatch.setenv('NEUTRAL_CREDENTIAL','synthetic-neutral-credential')
 token=ACTIVE_CONNECTION.set(None)
 try:
  result=trace('cid',state(),context(),details=True,created=NOW,
    reviews=[{'note':'echo synthetic-neutral-credential'}],
    feedback=[{'payload':{'note':'synthetic-neutral-credential'}}])
  assert 'synthetic-neutral-credential' not in json.dumps(result)
  assert result['reviewNotes'][0]['note']=='echo [REDACTED]'
 finally:ACTIVE_CONNECTION.reset(token)

def test_bounded_note_details_show_latest_records_and_signal_incomplete_view():
 notes=[{'note':f'합성 메모 {i}'} for i in range(41)]
 out=trace('cid',state(),context(),details=True,created=NOW,reviews=notes)
 assert len(out['reviewNotes'])==40
 assert out['reviewNotes'][0]['note']=='합성 메모 1'
 assert out['reviewNotes'][-1]['note']=='합성 메모 40'
 assert out['noteRecords']=={'reviewsTotal':41,'feedbackTotal':0,'limitPerKind':40,'truncated':True,'order':'latest retained records in stored order'}


def test_connection_revision_is_recorded_setting_identity_not_release_or_inferred_model():
 from app.request_audit import list_summary
 summary=list_summary({'model':'served-model','backend':'openai-compatible','connectionRevision':7,'revision':'release-label'})
 assert summary['connectionRevision']==7 and summary['backend']=='openai-compatible'
 assert summary['revision']=='release-label'
 assert list_summary({})['connectionRevision'] is None
 assert list_summary({'connectionRevision':True})['connectionRevision'] is None


def test_detail_retains_profile_and_complete_tool_evidence_only_with_admin_detail():
 c=context();c['recipientProfile']={'preferences':{'giftPreference':'합성 취향'},'apiKey':'fake-key'}
 c['diagnostics']['operations'][0]['result']['hits']=[{'title':'상품','description':'설명'}]
 out=trace('cid',state(),c,details=True,created=NOW)
 assert out['recipientProfile']['preferences']['giftPreference']=='합성 취향'
 assert out['recipientProfile']['apiKey']=='[REDACTED]'
 assert out['toolOperations'][0]['arguments']['query']=='PRIVATE_QUERY'
 assert out['toolOperations'][0]['result']['hits'][0]['description']=='설명'
 assert 'toolOperations' not in trace('cid',state(),c)
 old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
 assert 'toolOperations' not in trace('cid',state(),c,details=True,created=old)

def test_json_tool_arguments_and_system_state_remove_credentials_not_preferences():
 from app.diagnostics import clean
 body={'messages':[{'role':'system','content':'policy\nSTATE_JSON\n'+json.dumps({'recipient_preferences':{'giftPreference':'tea'},'accessToken':'TOKEN_CANARY'})},
  {'role':'assistant','reasoning':'HIDDEN_REASONING','tool_calls':[{'function':{'name':'search','arguments':json.dumps({'query':'tea','clientSecret':'SECRET_CANARY'})}}]}]}
 output=capture_request(body);wire=json.dumps(output)
 assert 'tea' in wire and 'TOKEN_CANARY' not in wire and 'SECRET_CANARY' not in wire
 assert 'HIDDEN_REASONING' not in wire
 assert clean({'refresh_token':'secret','Authorization':'Bearer x','private_key':'abc'})=={'refresh_token':'[REDACTED]','Authorization':'[REDACTED]','private_key':'[REDACTED]'}


@pytest.mark.parametrize('raw,secret',[
 ('Authorization: Basic dXNlcjpwYXNz','dXNlcjpwYXNz'),
 ('password="two word secret"','word secret'),
 ('https://u:fakepass@example.invalid','fakepass'),
])
def test_credential_prose_formats_are_filtered(raw,secret):
 from app.diagnostics import clean
 assert secret not in clean(raw)

def test_deep_untrusted_json_cannot_crash_audit_capture():
 result=capture_request({'messages':[{'role':'user','content':'['*1200+'0'+']'*1200}]})
 assert result['messages'][0]['role']=='user'
