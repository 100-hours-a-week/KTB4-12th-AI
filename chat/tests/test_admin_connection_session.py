"""Exercise the shipped JavaScript with delayed responses, without a browser/server."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_connection_responses_cannot_restore_logged_out_state():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is not installed')
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
class Element {
 constructor(){this.children=[];this.value='';this.disabled=false;}
 append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;}
 setAttribute(){}reset(){if(this.elements)for(const input of this.elements)input.value='';}
}
const nodes=new Map(),get=k=>{if(!nodes.has(k))nodes.set(k,new Element());return nodes.get(k);};
get('#filters').elements={status:{value:''}};
const fields=['baseUrl','model','connectTimeout','readTimeout'];const elements=fields.map(()=>new Element());fields.forEach((k,i)=>elements[k]=elements[i]);get('#connection-form').elements=elements;
const sandbox={document:{querySelector:get,querySelectorAll:()=>[],createElement:()=>new Element()},URLSearchParams,Date,JSON,Object,setTimeout,FormData:class{get(){return '';}},ChatHTTP:{json:async()=>({items:[],nextCursor:null})}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);const run=x=>vm.runInContext(x,sandbox);
const reply={revision:12345,config:{kind:'openrouter',model:'default',baseUrl:'https://openrouter.ai/api/v1'},openrouterReady:true,memoryOnly:true,endpointActive:false};
(async()=>{
 await new Promise(setImmediate);sandbox.reply=reply;run('showConnection(reply)');
 assert.equal(get('#connection-apply').disabled,false);
 // Editing never sends a request or changes actual status. Submission clears key.
 let sent=[];sandbox.testApi=async(path,options)=>{sent.push({path,...options});return {...reply,endpointActive:true,config:{kind:'openai-compatible',model:'test-model',baseUrl:'https://model.example/v1'}};};run('api=testApi');
 elements.baseUrl.value='https://model.example/v1';elements.model.value='test-model';elements.connectTimeout.value='10';elements.readTimeout.value='75';get('#modal-key').value='synthetic-key-not-for-echo';
 const submit=get('#connection-form').onsubmit({preventDefault(){}});assert.equal(get('#modal-key').value,'');await submit;
 assert.equal(sent.length,1);assert(sent[0].path.endsWith('/activate'));assert.equal(JSON.parse(sent[0].body).key,'synthetic-key-not-for-echo');
 assert.equal(get('#modal-key').value,'');assert(get('#connection-active').textContent.includes('test-model'));
 // Busy mutation blocks repeated submits, off and reads, without replay.
 let finish;sandbox.waiting=new Promise(resolve=>finish=resolve);run('api=()=>waiting');
 const mutation=run("connectionAction('/admin/api/model-connection/modal-off',{revision:12345})");
 assert.equal(get('#connection-apply').disabled,true);await run('loadConnection()');await get('#connection-form').onsubmit({preventDefault(){}});
 finish(reply);await mutation;assert.equal(run('connectionBusy'),false);
 // Lost mutation response is uncertain. Re-query reconciles; never auto-resend.
 run('api=async()=>{throw new Error("lost response") }');await run("connectionAction('/admin/api/model-connection/modal-off',{})");
 assert.equal(run('connectionUncertain'),true);assert.equal(get('#connection-apply').disabled,true);assert.equal(get('#connection-reload').disabled,false);
 run('api=async()=>reply');await run('loadConnection()');assert.equal(run('connectionUncertain'),false);
 // Late GET and mutation responses/errors cannot revive logged-out state.
 for(const expression of ['loadConnection()',"connectionAction('/admin/api/model-connection/modal-off',{})"]){for(const fail of [false,true]){
  let end;sandbox.deferred=new Promise((resolve,reject)=>end=fail?reject:resolve);run('showConnection(reply);api=()=>deferred');const pending=run(expression);run('resetSession()');end(fail?new Error('old'):reply);await pending;
  assert.equal(run('connectionSnapshot'),null);assert.equal(run('connectionBusy'),false);assert.equal(get('#connection-apply').disabled,true);assert.equal(get('#connection-status').textContent,'로그인이 필요합니다.');
 }}
 run('showConnection({...reply,openrouterReady:false})');assert.equal(get('#connection-apply').disabled,true);
 console.log('memory connection UI state transitions passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run([node,'-e',script,str(Path(__file__).parents[1]/'app/static/admin.js')],check=True,capture_output=True,text=True)
