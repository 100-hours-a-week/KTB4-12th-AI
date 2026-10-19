import test from 'node:test';
import assert from 'node:assert/strict';
import {usageView, initializeProviderUsage} from '../static/usage.js';

test('missing values never become zero or healthy', () => {
  assert.equal(usageView().usage,'미확인');
  assert.equal(usageView({state:'unconfigured'}).status,'키 미설정');
  assert.equal(usageView({state:'partial',usageUsd:null,limitUsd:null,remainingUsd:null}).remaining,'미확인');
});
test('zero and negative allowances remain visible', () => {
  const view=usageView({state:'verified',usageUsd:0,limitUsd:0,remainingUsd:-0.25});
  assert.equal(view.usage,'$0.00'); assert.equal(view.limit,'$0.00'); assert.equal(view.remaining,'$-0.25');
});
test('unlimited key allowance does not claim account balance', () => {
  const view=usageView({state:'unlimited',limitState:'unlimited',remainingUsd:null});
  assert.equal(view.status,'키 한도 없음'); assert.equal(view.remaining,'한도 없음');
});
test('five minute old values become stale locally without another request', () => {
  const data={state:'verified',ageSeconds:10,staleAfterSeconds:300};
  assert.equal(usageView(data,289).state,'verified');
  assert.equal(usageView(data,290).state,'stale');
});
test('failed refresh preserves failure even when old numeric amounts exist', () => {
  const view=usageView({state:'failed',ageSeconds:500,checkedAt:'2026-10-19T11:00:00Z',usageUsd:3});
  assert.equal(view.status,'조회 실패 · 이전 값'); assert.equal(view.usage,'$3.00');
});
test('reset window is distinct from lifetime usage', () => {
  assert.match(usageView({limitReset:'weekly'}).reset,/월요일 UTC/);
});

test('credential save clears old amounts even when the next HTTP request fails', async t => {
  const elements=new Map(), events=new Map();
  const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{}});return elements.get(id);};
  const oldDocument=Object.getOwnPropertyDescriptor(globalThis,'document');
  Object.defineProperty(globalThis,'document',{configurable:true,value:{getElementById:element,addEventListener:(name,fn)=>events.set(name,fn)}});
  t.after(()=>{if(oldDocument)Object.defineProperty(globalThis,'document',oldDocument);else delete globalThis.document;});
  t.mock.method(globalThis,'setInterval',()=>0);
  let fail=false;
  t.mock.method(globalThis,'fetch',async()=>{
    if(fail)throw new Error('synthetic failure');
    return new Response(JSON.stringify({chat:{state:'verified',usageUsd:7.5,checkedAt:'2026-10-19T11:00:00Z'},profiler:{state:'unconfigured'}}));
  });
  const settled=()=>new Promise(resolve=>setImmediate(resolve));
  initializeProviderUsage(); await settled();
  assert.equal(element('usage-chat-usage').textContent,'$7.50');
  fail=true; await element('refresh-provider-usage').onclick();
  assert.equal(element('usage-chat-usage').textContent,'$7.50'); // Same-key refresh keeps clearly labelled previous data.
  events.get('provider-key-change-finished')(); await settled();
  assert.equal(element('usage-chat-usage').textContent,'미확인');
  assert.equal(element('usage-chat-status').textContent,'조회 실패');
});

test('an uncertain credential write immediately invalidates visible and in-flight amounts', async t => {
  const elements=new Map(), events=new Map();
  const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{}});return elements.get(id);};
  const oldDocument=Object.getOwnPropertyDescriptor(globalThis,'document');
  Object.defineProperty(globalThis,'document',{configurable:true,value:{getElementById:element,addEventListener:(name,fn)=>events.set(name,fn)}});
  t.after(()=>{if(oldDocument)Object.defineProperty(globalThis,'document',oldDocument);else delete globalThis.document;});
  t.mock.method(globalThis,'setInterval',()=>0);
  let finish;
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{finish=resolve;}));
  initializeProviderUsage();
  events.get('provider-key-changing')();
  events.get('provider-key-changing')();
  finish(new Response(JSON.stringify({chat:{state:'verified',usageUsd:7.5},profiler:{state:'unconfigured'}})));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('usage-chat-usage').textContent,'미확인');
  assert.equal(element('usage-chat-status').textContent,'조회 전');
  events.get('provider-key-change-finished')();
  assert.equal(element('refresh-provider-usage').disabled,true);
  assert.equal(element('usage-chat-usage').textContent,'미확인');
  events.get('provider-key-change-finished')();
  finish(new Response(JSON.stringify({chat:{state:'verified',usageUsd:2},profiler:{state:'unconfigured'}})));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('usage-chat-usage').textContent,'$2.00');
  assert.equal(element('refresh-provider-usage').disabled,false);
});
