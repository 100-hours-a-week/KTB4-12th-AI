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
get('#connection-form').elements={kind:{value:'openrouter'},auth:{value:'existing-openrouter'},secretRef:new Element()};
const sandbox={document:{querySelector:get,querySelectorAll:()=>[],createElement:()=>new Element()},
 URLSearchParams,Date,JSON,Object,setTimeout,FormData:class{get(){return '';}},Option:class{},
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
 assert.equal(get('#login').hidden,false);assert.equal(get('#workspace').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script,str(Path(__file__).resolve().parents[1]/'app/static/admin.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
