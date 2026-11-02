"""Exercise the shipped JavaScript with delayed responses, without a browser/server."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_connection_responses_cannot_restore_logged_out_state():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is not installed')
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
class Element {
 constructor(){this.children=[];this.value='';this.checked=false;this.resetCount=0;}
 append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x;}
 setAttribute(){} reset(){this.resetCount++;}
}
const nodes=new Map(),get=k=>{if(!nodes.has(k))nodes.set(k,new Element());return nodes.get(k);};
get('#filters').elements={status:{value:''}};
get('#connection-form').elements={kind:{value:'openrouter'},auth:{value:'existing-openrouter'},secretRef:new Element()};
const sandbox={document:{querySelector:get,querySelectorAll:()=>[],createElement:()=>new Element()},
 crypto:{randomUUID:()=> 'new-profile'},URLSearchParams,Date,JSON,Object,setTimeout,FormData:class{constructor(form){this.form=form;}get(key){return this.form.elements?.[key]?.value||'';}},Option:class{},
 ChatHTTP:{json:async()=>({items:[],nextCursor:null})}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const run=x=>vm.runInContext(x,sandbox);
const reply={revision:1,config:{kind:'openrouter',model:'synthetic'},writable:true,history:[],secretReferences:[]};
(async()=>{
 await new Promise(setImmediate);
 sandbox.reply=reply;run('showConnection(reply)');
 assert.equal(run('connectionSnapshot.revision'),1);
 // Disabled fixed-auth fields must still be serialized when saving OpenRouter.
 Object.assign(get('#connection-form').elements,{baseUrl:{value:'https://openrouter.ai/api/v1'},model:{value:'synthetic'},connectTimeout:{value:'10'},readTimeout:{value:'75'}});
 run('connectionButtons()');assert.equal(get('#connection-form').elements.auth.disabled,true);
 assert.equal(run('connectionInput().auth'),'existing-openrouter');
 get('#connection-form').elements.secretRef.value='STALE_REFERENCE';
 assert.equal(run('connectionInput().secretRef'),null);
 run('resetSession()');assert.equal(run('connectionSnapshot'),null);
 assert.equal(get('#connection-capabilities').children.length,0);
 assert.equal(get('#connection-save').disabled,true);
 // Unknown/synthetic captures must not claim an actual model transmission.
 const texts=n=>[n.textContent||'',...n.children.flatMap(texts)].join(' ');
 sandbox.traceFixture={recording:{},modelCalls:[{index:1,input:{capture:'scripted-preview-only'}}]};
 assert(!texts(run('traceDetail(traceFixture)')).includes('실제 전송 시점'));
 sandbox.traceFixture.modelCalls[0].input.capture='actual-send-time-redacted';
 assert(texts(run('traceDetail(traceFixture)')).includes('실제 전송 시점'));
 // Named profiles: choosing/editing never applies or probes a model.
 const router={kind:'openrouter',baseUrl:'https://openrouter.ai/api/v1',model:'openai/gpt-6-luna',auth:'existing-openrouter',secretRef:null,connectTimeout:10,readTimeout:75};
 const modal={...router,kind:'openai-compatible',baseUrl:'https://modal.example/v1',model:'served-model',auth:'none'};
 sandbox.named={revision:2,config:router,defaultConfig:router,activeProfileId:'openrouter',writable:true,history:[],secretReferences:[],profiles:[{id:'openrouter',name:'OpenRouter',config:router,verification:{}},{id:'modal',name:'Modal Qwen',config:modal,verification:{state:'verified',tools:true,sse:true}}]};
 const profileWrites=[];sandbox.profileApi=async(path,options)=>{profileWrites.push({path,...options});return sandbox.named;};run('api=profileApi');
 run('showConnection(named)');get('#connection-profile').value='modal';run('chooseProfile()');
 assert.equal(profileWrites.length,0);assert.equal(run('connectionSnapshot.activeProfileId'),'openrouter');
 assert.equal(get('#connection-apply').disabled,false);
 get('#connection-form').elements.model.value='edited-model';get('#connection-form').oninput();
 assert.equal(get('#connection-apply').disabled,true);
 run('chooseProfile()');await get('#connection-apply').onclick();await new Promise(setImmediate);
 assert.equal(profileWrites.length,1);assert.equal(profileWrites[0].method,'POST');assert(profileWrites[0].path.endsWith('/profiles/modal/apply'));
 get('#connection-profile').value='';run('chooseProfile()');get('#connection-profile-name').value='New Modal';
 get('#connection-form').onsubmit({preventDefault(){}});await new Promise(setImmediate);
 assert.equal(profileWrites.length,2);assert.equal(profileWrites[1].method,'PUT');assert(profileWrites[1].path.endsWith('/profiles/new-profile'));
 assert.equal(JSON.parse(profileWrites[1].body).name,'New Modal');
 assert(!profileWrites.some(r=>r.path.endsWith('/check')));
 sandbox.detached={...sandbox.named,config:modal,activeProfileId:null};run('showConnection(detached)');
 assert(get('#connection-active').textContent.includes('served-model'));assert(get('#connection-active').textContent.includes('https://modal.example/v1'));
 get('#connection-profile').value='openrouter';run('chooseProfile()');assert(get('#connection-active').textContent.includes('served-model'));
 run('resetSession()');assert.equal(get('#connection-profile-name').value,'');assert.equal(get('#connection-active').textContent,'');assert.equal(run('connectionSnapshot'),null);
 // Both read and mutation responses (including errors) may arrive after logout.
 for(const expression of ['loadConnection()',"connectionAction('/admin/api/model-connection',{})"]){
  for(const fails of [false,true]){
   let finish;sandbox.deferred=new Promise((resolve,reject)=>{finish=fails?reject:resolve;});
   run('api=()=>deferred');const pending=run(expression);
   run('resetSession()');finish(fails?new Error('OLD_RESPONSE'):reply);await pending;
   assert.equal(run('connectionSnapshot'),null);
   assert.equal(run('connectionBusy'),false);
   assert.equal(get('#connection-status').textContent,'로그인이 필요합니다.');
   assert.equal(get('#connection-save').disabled,true);
  }
 }
 // Reopening the model tab must not replace an in-flight explicit probe.
 run('showConnection(reply)');
 get('#connection-form').elements.kind.value='openai-compatible';
 get('#probe-consent').checked=true;
 let finishProbe;const probePaths=[];
 sandbox.probeApi=path=>{probePaths.push(path);return new Promise(resolve=>{finishProbe=resolve;});};
 run('api=probeApi');
 const probe=run("connectionAction('/admin/api/model-connection/check',{})");
 const probeGeneration=run('connectionVersion');
 await run('loadConnection()');
 assert.equal(probePaths.length,1);
 assert.equal(run('connectionVersion'),probeGeneration);
 assert.equal(run('connectionBusy'),true);
 assert.equal(get('#connection-check').disabled,true);
 finishProbe({state:'verified',checkedAt:1700000000});await probe;
 assert.equal(run('connectionBusy'),false);
 assert.equal(get('#connection-status').textContent,'호환 확인 완료 · 아직 저장하거나 적용하지 않았습니다.');
 // Draft cancel is local-only and keeps the applied model unchanged.
 run('showConnection(named)');get('#connection-profile').value='modal';run('chooseProfile()');
 const writesBeforeCancel=profileWrites.length;
 get('#connection-form').elements.model.value='discard-me';get('#connection-form').oninput();get('#connection-cancel').onclick();
 assert.equal(get('#connection-form').elements.model.value,'served-model');assert.equal(profileWrites.length,writesBeforeCancel);
 assert.equal(run('connectionSnapshot.config.kind'),'openrouter');
 // A lost mutation receipt blocks writes until explicit GET reconciliation.
 let mutationCount=0;sandbox.lostApi=async()=>{mutationCount++;throw new Error('synthetic response lost');};run('api=lostApi');
 await run("connectionAction('/admin/api/model-connection/profiles/modal/apply',{revision:2})");
 assert.equal(run('connectionUncertain'),true);assert.equal(get('#connection-apply').disabled,true);
 assert(get('#connection-status').textContent.includes('적용 여부 미확정'));assert(get('#connection-active').textContent.startsWith('마지막 확인:'));
 await run("connectionAction('/admin/api/model-connection',{})");assert.equal(mutationCount,1);
 sandbox.readback=async()=>sandbox.named;run('api=readback');await run('loadConnection()');assert.equal(run('connectionUncertain'),false);
 // Explicit validation rejection does not claim a switch or lock out recovery.
 sandbox.badApi=async()=>{const error=new Error('safe invalid settings');error.status=400;throw error;};run('api=badApi');
 await run("connectionAction('/admin/api/model-connection',{})");assert.equal(run('connectionUncertain'),false);assert.equal(run('connectionSnapshot.config.kind'),'openrouter');
 // Valid JSON with the wrong mutation contract is also an uncertain receipt.
 sandbox.malformedApi=async()=>({ok:true});run('api=malformedApi');
 await run("connectionAction('/admin/api/model-connection',{})");assert.equal(run('connectionUncertain'),true);
 await run('loadConnection()');assert.equal(run('connectionUncertain'),true);
 run('api=readback');await run('loadConnection()');assert.equal(run('connectionUncertain'),false);
 run('api=malformedApi');await run("connectionAction('/admin/api/model-connection/check',{})");
 assert(!get('#connection-status').textContent.includes('호환 확인 완료'));
 // Manual native recovery uses server default config, not an editable profile id.
 const recoverWrites=[];sandbox.recoveryApi=async(path,options)=>{recoverWrites.push({path,...options});return sandbox.named;};run('api=recoveryApi');
 get('#connection-openrouter').onclick();await new Promise(setImmediate);
 assert.equal(JSON.parse(recoverWrites[0].body).config.kind,'openrouter');assert.equal(recoverWrites[0].path,'/admin/api/model-connection');
 // Past probe timestamp is explicit; no automatic liveness claim.
 sandbox.proof={state:'verified',checkedAt:1700000000};assert(run('compatibilityLabel(proof)').includes('현재 연결 상태를 보장하지 않습니다'));
 // Fast filters use the actual status query, starting a fresh page.
 const queries=[];sandbox.filterApi=async path=>{queries.push(path);return {items:[],nextCursor:null};};run('api=filterApi');
 get('#requests-failed').onclick();await new Promise(setImmediate);assert(queries.at(-1).includes('status=FAILED'));
 get('#requests-cancelled').onclick();await new Promise(setImmediate);assert(queries.at(-1).includes('status=CANCELLED'));
 // Keep the real api wrapper: an old 401 cannot invalidate a new login.
 const source=fs.readFileSync(process.argv[1],'utf8');
 run(source.slice(source.indexOf('async function api('),source.indexOf('function resetSession(')));
 let oldUnauthorized,finishOld;
 sandbox.ChatHTTP.json=(path,options,onUnauthorized)=>{
  if(path==='/old-session'){oldUnauthorized=onUnauthorized;return new Promise(resolve=>{finishOld=resolve;});}
  return Promise.resolve({items:[],nextCursor:null});
 };
 const old=run("api('/old-session')");
 // Log out, then perform the real login handler (which establishes a new epoch).
 run('resetSession()');
 await get('#login').onsubmit({preventDefault(){}});
 run('showConnection(reply)');
 oldUnauthorized();finishOld({});await old;
 assert.equal(run('connectionSnapshot.revision'),1);
 assert.equal(get('#workspace').hidden,false);
 // Even initial anonymous requests must not invalidate a later successful login.
 const anonymous=run("api('/old-session')");
 await get('#login').onsubmit({preventDefault(){}});run('showConnection(reply)');
 oldUnauthorized();finishOld({});await anonymous;
 assert.equal(run('connectionSnapshot.revision'),1);
 // A current-session 401 still signs out and clears state.
 const active=run("api('/old-session')");oldUnauthorized();finishOld({});await active;
 assert.equal(run('connectionSnapshot'),null);
 assert.equal(get('#login').hidden,false);assert.equal(get('#workspace').hidden,true);assert(get('#status').textContent.includes('관리자'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script,str(Path(__file__).resolve().parents[1]/'app/static/admin.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
