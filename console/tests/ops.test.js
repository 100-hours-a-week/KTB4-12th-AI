import test from 'node:test';import assert from 'node:assert/strict';
import {money,acceptSnapshot,elapsedValue,httpSummary,displayedClock} from '../static/ops.js';
import {redact,httpBody,eventPreview} from '../static/core.js';
test('unknown provider cost is not invented as zero',()=>{assert.equal(money(null),'미수집');assert.equal(money(undefined),'미수집');assert.equal(money(0),'$0.00');});
test('late snapshot never overwrites a more recent observation',()=>{assert.equal(acceptSnapshot({collectedAt:'2026-10-15T01:00:02Z'},{collectedAt:'2026-10-15T01:00:01Z'}).ok,false);assert.equal(acceptSnapshot(null,{}).ok,false);assert.equal(acceptSnapshot(null,{collectedAt:'2026-10-15T01:00:02Z'}).ok,true);});
test('elapsed is one decimal, terminal and stale never keep running',()=>{assert.equal(elapsedValue({elapsedSeconds:3.2,active:true},.1),'3.3s');assert.equal(elapsedValue({elapsedSeconds:3.2,active:false},99),'3.2s');assert.equal(elapsedValue({elapsedSeconds:3.2,active:true},99,true),'3.2s');assert.equal(elapsedValue({elapsedSeconds:null,active:true},2),'미계측');});
test('relay and upstream throttling count one failed request',()=>{const r=httpSummary([{relayStatus:429,transport:{exchanges:[{status:429}]}},{relayStatus:200,transport:{exchanges:[{status:503}]}}]);assert.equal(r.throttled,1);assert.equal(r.unavailable,1);});
test('prompt, provider response, error and callback query canaries never leak through raw expansion',()=>{const payload={prompt:'PRIVATE_PROMPT',providerResponse:'PRIVATE_PROVIDER',callbackUrl:'http://private.invalid?token=PRIVATE_QUERY',error:{code:'PRIVATE_CODE',message:'PRIVATE_ERROR',providerResponse:'PRIVATE_BODY'},detail:'PRIVATE_DETAIL'};for(const reveal of [false,true]){const result=JSON.stringify(redact(payload,reveal));assert.ok(!/PRIVATE_/.test(result));}assert.ok(!/PRIVATE_/.test(JSON.stringify(httpBody(payload))));assert.ok(!/PRIVATE_/.test(eventPreview({event:'console.error',data:{error:payload.error}},true).body));});

test('render between ticks freezes the latest visible tenth on outage',()=>{const key='Chat/render-between-ticks';assert.equal(displayedClock(key,1.2,true,0),1.2);assert.equal(displayedClock(key,1.2,true,.09),1.3);assert.equal(displayedClock(key,1.2,true,.095,true),1.3);assert.equal(displayedClock(key,1.2,true,.1,true),1.3);assert.equal(displayedClock(key,1.2,true,0),1.3);assert.equal(displayedClock(key,.9,false,99),.9);assert.equal(displayedClock(key,null,true,99),null);});

import {requestErrorMessage,parseProfileJSON} from '../static/core.js';
test('validation errors identify safe fields without echoing private input',()=>{const error={detail:[{loc:['body','reviews',0,'rating'],msg:'PRIVATE_MSG',input:'PRIVATE_INPUT'},{loc:['body','PRIVATE_FIELD'],msg:'PRIVATE_DETAIL'}]};const message=requestErrorMessage(422,error);assert.match(message,/리뷰/);assert.match(message,/별점/);assert.ok(!message.includes('PRIVATE'));assert.match(requestErrorMessage(429,{}),/요청 한도/);assert.throws(()=>parseProfileJSON('PRIVATE_JSON','리뷰'),/리뷰는 JSON 배열/);assert.throws(()=>parseProfileJSON('{}','리뷰'),/JSON 배열/);assert.deepEqual(parseProfileJSON('[]','리뷰'),[]);});


import {connectionView,copyIntegrationAddress,safeIntegrationAddress,renderIntegration} from '../static/ops.js';
test('connection information distinguishes settings, observations, and unknown BE connectivity',()=>{
 const view=connectionView({environment:'develop',revision:'abcdef123456',revisionSource:'console-build',readiness:{database:'ready',products:'empty'}},{chat:true,profiler:false,backend:null});
 assert.equal(view.environment,'개발');assert.match(view.revision,/Console build/);
 assert.match(view.keys,/Chat 설정됨/);assert.match(view.keys,/Profiler 미설정/);assert.match(view.token,/미수집/);
 assert.equal(view.database,'DB 준비됨');assert.equal(view.products,'상품 데이터 상품 없음');
 assert.equal(connectionView(null).environment,'환경 미확인');
 assert.equal(connectionView({readiness:{database:'ready'}},{},true).database,'DB 이전 관측');
});
test('copy never accepts credentials, queries, or missing addresses and reports clipboard failure',async()=>{
 const writes=[],clipboard={writeText:async value=>writes.push(value)};
 for(const value of [null,'','javascript:alert(1)','https://user:PRIVATE@example.invalid','https://example.invalid?token=PRIVATE','https://example.invalid#PRIVATE','https://example.invalid/%50RIVATE']){
  assert.equal(safeIntegrationAddress(value),false);assert.equal(await copyIntegrationAddress(value,clipboard),false);
 }
 const address='https://ai.example.invalid/v1/chat/conversations/{conversationId}';
 assert.equal(await copyIntegrationAddress(address,clipboard),true);assert.deepEqual(writes,[address]);
 assert.equal(await copyIntegrationAddress(address,{writeText:async()=>{throw Error('PRIVATE');}}),false);
});
test('connection panel hides invalid URLs and enables only validated copy actions',async t=>{
 const elements=new Map();
 const node=tag=>({tag,textContent:'',children:[],isConnected:true,append(...items){this.children.push(...items)},replaceChildren(){this.children=[]},setAttribute(name,value){this[name]=value}});
 const element=id=>{if(!elements.has(id))elements.set(id,node('div'));return elements.get(id)};
 const old=globalThis.document;t.after(()=>{if(old===undefined)delete globalThis.document;else globalThis.document=old});
 globalThis.document={getElementById:element,createElement:node};
 renderIntegration({environment:'production',api:{chat:{state:'configured',url:'https://ai.example.invalid/route',method:'PUT'},profiler:{state:'configured',url:'https://user:PRIVATE@example.invalid?token=PRIVATE',method:'POST'}}});
 const rows=element('integration-api').children;
 assert.equal(rows[0].children[1].disabled,false);assert.equal(rows[1].children[1].disabled,true);
 assert.ok(!JSON.stringify(rows).includes('PRIVATE'));assert.equal(element('integration-environment').textContent,'운영');
 renderIntegration(undefined);assert.ok(element('integration-api').children.every(row=>row.children[1].disabled));
});
