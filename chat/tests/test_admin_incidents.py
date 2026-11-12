import os
from pathlib import Path
import shutil
import subprocess
import httpx
import pytest
from app import main, security


async def test_incident_list_requires_admin_and_forwards_filters_without_request_id(monkeypatch):
 calls=[]
 class Store:
  async def call(self,method,**kwargs):
   calls.append((method,kwargs));return {'items':[],'nextCursor':None}
 monkeypatch.setattr(main.app.state,'store',Store(),raising=False);security.rate.clear()
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  assert (await client.get('/admin/api/conversations?issue=attention')).status_code==401
  assert not calls
  assert (await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})).status_code==200
  assert (await client.get('/admin/api/conversations?issue=attention')).status_code==200
  assert calls[-1][1]['issue']=='attention' and calls[-1][1]['sort']=='activity'
  assert not calls[-1][1]['q']
  before=len(calls)
  assert (await client.get('/admin/api/conversations?issue=invalid')).status_code==400
  assert len(calls)==before


def test_incident_ui_filters_focus_and_diagnostics_use_actual_controller():
 node=shutil.which('node')
 if not node:pytest.skip('Node.js required')
 script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.dataset={};this.textContent='';}
 append(...x){this.children.push(...x);} prepend(...x){this.children.unshift(...x);} replaceChildren(...x){this.children=x;}
 setAttribute(){} set innerHTML(x){throw Error('HTML parsing forbidden');}
}
const nodes=new Map(),get=k=>{if(!nodes.has(k))nodes.set(k,new Element());return nodes.get(k);};
get('#connection-form').elements={};
get('#filters').elements=Object.fromEntries(['status','issue','sort','q','feedback'].map(k=>[k,{value:''}]));
const requests=[],sandbox={document:{querySelector:get,querySelectorAll:()=>[],createElement:tag=>new Element(tag)},URLSearchParams,Date,JSON,Object,setTimeout,
 FormData:class{constructor(form){this.form=form;}get(k){return this.form.elements[k]?.value||'';}},
 ChatHTTP:{json:async path=>{requests.push(path);return {items:[],nextCursor:null};}}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);const run=x=>vm.runInContext(x,sandbox);
const text=e=>e.textContent+' '+e.children.map(text).join(' ');
(async()=>{
 await new Promise(setImmediate);
 get('#filters').elements.status.value='COMPLETED';get('#requests-attention').onclick();await new Promise(setImmediate);
 assert(requests.at(-1).includes('issue=attention'));assert(!requests.at(-1).includes('status='));assert(requests.at(-1).includes('sort=activity'));
 get('#requests-processing').onclick();await new Promise(setImmediate);assert(requests.at(-1).includes('status=PROCESSING'));assert(!requests.at(-1).includes('issue='));
 get('#requests-all').onclick();await new Promise(setImmediate);assert(!requests.at(-1).includes('issue='));assert(!requests.at(-1).includes('status='));
 sandbox.record={diagnostic:{category:'provider_read_timeout',code:'ReadTimeout',stage:'model_2',turnTimeoutSeconds:180},recording:{calls:'recorded-redacted'},totals:{},modelCalls:[],timeline:[{kind:'model',index:2,httpStatus:200,headersMs:3,errorType:'ReadTimeout',timeouts:{readSeconds:75}}]};
 const panel=run('tracePanel("cid",{turnId:"tid",context:{},trace:record})');const rendered=text(panel);
 for(const expected of ['모델 응답 읽기 시간 초과','model_2','ReadTimeout','HTTP 200','첫 패킷 미기록','BE·사용자 수신 여부','진단 기록 저장'])assert(rendered.includes(expected),expected);
 assert(!rendered.includes('Invalid Date'));assert.equal(run('when(null)'),'미기록');
 sandbox.record={diagnostic:{category:'recipient_profile_unavailable',code:'recipient_profile_unavailable',stage:'recipient_profile',httpStatus:503,httpStatusSource:'application'},sourceIdentity:{promptVersion:'ac341aa9ee732941',buildCommit:'a'.repeat(40)},recording:{calls:'not-recorded-legacy'},timeline:[],totals:{}};
 const failed=text(run('tracePanel("cid",{turnId:"tid",trace:record})'));
 for(const expected of ['수신자 취향 조회 실패','내부 상태 503','코드 지문 ac341aa9ee73','빌드 커밋 aaaaaaaaaaaa','코드 지문은 Git 커밋이 아닙니다'])assert(failed.includes(expected),expected);
 sandbox.record={status:'FAILED',diagnostic:{code:'APIError',stage:'context'}};const old=text(run('diagnosticPanel(record)'));assert(old.includes('HTTP 상태 응답 없음 또는 미기록'));assert(!old.includes('일부 실패 정보가 저장되지 않았습니다'));
 sandbox.record={status:'FAILED',diagnostic:{}};assert(text(run('diagnosticPanel(record)')).includes('누락된 원인은 이 기록만으로 복원할 수 없습니다'));
 sandbox.record={};assert(text(run('diagnosticPanel(record)')).includes('원인 정보 미기록'));
 console.log('Incident filters and diagnostic rendering passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
 subprocess.run([node,'-e',script,str(Path(__file__).parents[1]/'app/static/admin.js')],check=True,capture_output=True,text=True)
