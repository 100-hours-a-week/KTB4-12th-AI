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
get('#connection-form').elements={kind:{value:'openrouter'},secretRef:new Element()};
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
 run('resetSession()');assert.equal(run('connectionSnapshot'),null);
 assert.equal(get('#connection-capabilities').children.length,0);
 assert.equal(get('#connection-save').disabled,true);
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
 assert.equal(get('#login').hidden,false);assert.equal(get('#workspace').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script,str(Path(__file__).resolve().parents[1]/'app/static/admin.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
