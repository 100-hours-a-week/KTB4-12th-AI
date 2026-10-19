import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import * as core from '../static/core.js';

const settle = () => new Promise(resolve => setImmediate(resolve));

function screen({acceptance = 502, stored = 'PROCESSING', config, post, quickPost, storage=new Map(), scope='synthetic', read} = {}) {
  const elements = new Map(), requests = [], listeners = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value:id === 'message' ? 'SYNTHETIC' : 'all',textContent:'',disabled:false,checked:false,
      children:[],classList:{add(){},remove(){},toggle(){},contains(){return false;}},
      append(){},replaceChildren(){},setAttribute(){},getAttribute(){return '';},addEventListener(){}});
    return elements.get(id);
  };
  let stream;const streams=[];
  const sessionStorage={get length(){return storage.size;},key:index=>[...storage.keys()][index]??null,
    getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  const context = vm.createContext({...core,crypto:webcrypto,performance,AbortController,TextDecoder,URLSearchParams,
    sessionStorage,
    document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>element(Math.random()),addEventListener:(name,listener)=>listeners.set(name,listener)},
    location:{search:''},setInterval(){},setTimeout(){},clearInterval(){},clearTimeout(){},
    initializeProviderUsage(){},createProductLoader:fn=>fn,createProductCards(){},renderOperations(){},tickOperations(){},acceptSnapshot(){},
    adminFetch:async(path,options={})=>{
      requests.push([options.method ?? 'GET',path,options.body?JSON.parse(options.body):null]);
      if (path === '/api/config') return config ? config() : Response.json({mode:'LOCAL_TARGET',profileSendAllowed:false,chatSendAllowed:true});
      if (path === '/api/chat') return post ? post() : Response.json({detail:'SYNTHETIC'}, {status:acceptance});
      if (path === '/api/chat/quick-check') return quickPost ? quickPost(JSON.parse(options.body)) : Response.json({detail:'SYNTHETIC'}, {status:acceptance});
      if (path.includes('/events?')) return new Response(new ReadableStream({start(controller){stream=controller;streams.push(controller);}}));
      if (path.startsWith('/api/chat/') && read)return read(path,options);
      if (path.startsWith('/api/chat/')) return stored
        ? Response.json({data:{status:stored,lastEventId:2}}) : Response.json({}, {status:404});
      throw new Error('Unexpected synthetic request');
    },
  });
  const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8')
    .replace(/^import .+?;$/gm,'').replace(/^const adminSession=await initializeAdmin\(\);$/m,'')
    .replace(/^const chatStorageKey=.+$/m,`const chatStorageKey=${JSON.stringify('console-chat/'+scope)};`)
    + '\nevents=()=>{};eventDetail=()=>{};renderHTTP=()=>{};globalThis.probe={sendChat,state,newChat,chatContext,record,connect,refreshDiagnostics,renderChatResult,observeImage};';
  vm.runInContext(source,context,{filename:'console/static/app.js'});
  return {element,requests,storage,probe:context.probe,settingsChanged:()=>listeners.get('admin-settings-saved')(),
    finish(index=streams.length-1,status='COMPLETED'){
      const target=streams[index];target.enqueue(new TextEncoder().encode('id: 1\nevent: answer.delta\ndata: {"delta":"Synthetic"}\n\nid: 2\nevent: turn.finished\ndata: '+JSON.stringify({status})+'\n\n'));
      target.close();
    }};
}

test('lost acceptance response recovers the existing stream and cancellation controls without resending', async () => {
  const ui = screen();
  await settle();
  await ui.probe.sendChat(false);
  await settle();
  assert.equal(ui.probe.state.admitted,true);
  assert.equal(ui.probe.state.status,'PROCESSING');
  assert.equal(ui.probe.state.ended,null);
  assert.equal(ui.element('cancel-chat').disabled,false);
  assert.equal(ui.element('disconnect').disabled,false);
  assert.equal(ui.requests.filter(([method])=>method === 'POST').length,1);
  const streamPath = ui.requests.find(([,path])=>path.includes('/events?'))[1];
  assert.equal(streamPath,`/api/chat/${ui.probe.state.plan.turn_id}/events?afterEventId=0`);
  ui.finish();
  await settle();
  await settle();
  assert.equal(ui.probe.state.status,'COMPLETED');
  assert.equal(ui.probe.state.log.cursor,2);
  assert.equal(ui.element('cancel-chat').disabled,true);
  assert.equal(ui.element('reconnect').disabled,false);
  assert.equal(ui.requests.filter(([method])=>method === 'POST').length,1);
});

test('an uncertain send with no saved turn preserves explicit retry and cannot open a stream', async () => {
  const ui = screen({acceptance:503,stored:null});
  await settle();
  await ui.probe.sendChat(false);
  assert.equal(ui.probe.state.admitted,false);
  assert.equal(ui.element('retry-chat').disabled,false);
  assert.equal(ui.element('reconnect').disabled,true);
  assert.equal(ui.element('cancel-chat').disabled,true);
  assert.equal(ui.requests.filter(([method,path])=>method === 'GET' && path.startsWith('/api/chat/')).length,1);
  assert.equal(ui.requests.some(([,path])=>path.includes('/events?')),false);
});

test('a definitive validation rejection never starts recovery reads or generation', async () => {
  const ui = screen({acceptance:422});
  await settle();
  await ui.probe.sendChat(false);
  assert.equal(ui.probe.state.admitted,false);
  assert.equal(ui.requests.filter(([method,path])=>method === 'GET' && path.startsWith('/api/chat/')).length,0);
  assert.equal(ui.element('retry-chat').disabled,false);
});

test('saving settings rechecks readiness while preserving the draft and current turn', async () => {
  let ready=false;
  const ui=screen({config:()=>Response.json({paidCallsEnabled:true,budgetMode:'provider-key-limits',chatSendAllowed:ready,profileSendAllowed:ready})});
  await settle();
  assert.equal(ui.element('send-chat').disabled,true);
  ui.element('message').value='An edited synthetic draft';
  ui.element('recipient').value='731';
  const plan={turn_id:'existing-turn'};ui.probe.state.plan=plan;
  ready=true;await ui.settingsChanged();
  assert.equal(ui.element('send-chat').disabled,false);
  assert.equal(ui.element('send-profile').disabled,false);
  assert.equal(ui.element('message').value,'An edited synthetic draft');
  assert.equal(ui.element('recipient').value,'731');
  assert.equal(ui.probe.state.plan,plan);
  assert.deepEqual(ui.requests.map(([method,path])=>[method,path]),[['GET','/api/config'],['GET','/api/config']]);
});

test('an older readiness response cannot reopen admission after a newer blocked response', async () => {
  let reads=0;
  const pending=[];
  const ui=screen({config:()=>++reads===1?Response.json({chatSendAllowed:true,profileSendAllowed:true}):new Promise(resolve=>pending.push(resolve))});
  await settle();
  const older=ui.settingsChanged(), newer=ui.settingsChanged();
  assert.equal(ui.element('send-chat').disabled,true);
  pending[1](Response.json({chatSendAllowed:false,profileSendAllowed:false}));await newer;
  pending[0](Response.json({chatSendAllowed:true,profileSendAllowed:true}));await older;
  assert.equal(ui.element('send-chat').disabled,true);
  assert.equal(ui.element('send-profile').disabled,true);
});

test('a failed readiness refresh keeps new requests disabled and never sends a model request', async () => {
  let failed=false;
  const ui=screen({config:()=>failed?Response.json({}, {status:503}):Response.json({chatSendAllowed:true,profileSendAllowed:true})});
  await settle();
  failed=true;await ui.settingsChanged();
  assert.equal(ui.element('send-chat').disabled,true);
  assert.equal(ui.element('send-profile').disabled,true);
  assert.ok(ui.element('toast').textContent);
  assert.equal(ui.requests.some(([method])=>method!=='GET'),false);
});

test('readiness refresh and acceptance cleanup cannot unlock busy or newly blocked send controls', async () => {
  let ready=true, finish;
  const ui=screen({stored:null,config:()=>Response.json({chatSendAllowed:ready,profileSendAllowed:ready}),post:()=>new Promise(resolve=>{finish=resolve;})});
  await settle();
  const sending=ui.probe.sendChat(false);
  ui.probe.state.profileSending=true;
  await ui.settingsChanged();
  assert.equal(ui.element('send-chat').disabled,true);
  assert.equal(ui.element('send-profile').disabled,true);
  ready=false;await ui.settingsChanged();
  finish(Response.json({}, {status:503}));await sending;
  assert.equal(ui.probe.state.sending,false);
  assert.equal(ui.element('send-chat').disabled,true);
  assert.equal(ui.element('retry-chat').disabled,false);
});

test('retry keeps its frozen binding; recipient change clears the previous request and conversation', async () => {
  const ui=screen({stored:null,config:()=>Response.json({paidCallsEnabled:true,chatSendAllowed:true,
    defaultSenderUserId:701,defaultRecipientUserId:801,chatRecipients:[
      {label:'Synthetic one',senderUserId:701,recipientUserId:801},
      {label:'Synthetic two',senderUserId:702,recipientUserId:802}]})});
  await settle();
  await ui.probe.sendChat(false);
  const original=JSON.stringify(ui.probe.state.plan);
  assert.equal(ui.probe.state.plan.senderUserId,701);
  assert.equal(ui.probe.state.plan.recipientUserId,801);
  await ui.probe.sendChat(true);
  assert.equal(JSON.stringify(ui.probe.state.plan),original);
  ui.element('chat-recipient').value='1';ui.element('chat-recipient').onchange();
  assert.equal(ui.probe.state.plan,null);
  assert.equal(ui.probe.state.log.items.length,0);
  assert.equal(ui.element('message').value,'');
  const count=ui.requests.length;await ui.probe.sendChat(true);assert.equal(ui.requests.length,count);
  ui.element('message').value='For the second synthetic recipient';
  await ui.probe.sendChat(false);
  assert.equal(ui.probe.state.plan.senderUserId,702);
  assert.equal(ui.probe.state.plan.recipientUserId,802);
  assert.notEqual(ui.probe.state.plan.conversation_id,JSON.parse(original).conversation_id);
});

const accepted=()=>Response.json({httpStatus:202,body:{data:{status:'PROCESSING'}}});

test('two completed turns keep a conversation but get distinct turn/request IDs; new conversation resets it',async()=>{
 const ui=screen({post:accepted,stored:'COMPLETED'});await settle();
 await ui.probe.sendChat(false);await settle();const first={...ui.probe.state.plan};
 assert.equal(ui.element('send-chat').disabled,true);
 await ui.probe.sendChat(false);assert.equal(ui.requests.filter(([method])=>method==='POST').length,1);
 ui.finish();await settle();await settle();assert.equal(ui.element('send-chat').disabled,false);
 ui.element('message').value='Follow-up';await ui.probe.sendChat(false);await settle();const second={...ui.probe.state.plan};
 assert.equal(second.conversation_id,first.conversation_id);
 assert.notEqual(second.turn_id,first.turn_id);assert.notEqual(second.request_id,first.request_id);
 ui.finish();await settle();await settle();
 ui.probe.newChat();assert.equal(ui.probe.state.plan,null);assert.equal(ui.storage.size,0);
 ui.element('message').value='A new discussion';await ui.probe.sendChat(false);
 assert.notEqual(ui.probe.state.plan.conversation_id,first.conversation_id);
});

test('reload restores the frozen turn by GET/SSE only and a follow-up keeps its conversation',async()=>{
 const storage=new Map(),first=screen({storage,post:accepted,stored:'COMPLETED'});await settle();
 await first.probe.sendChat(false);await settle();const original={...first.probe.state.plan};first.finish();await settle();await settle();
 const loaded=screen({storage,post:accepted,stored:'COMPLETED'});await settle();await settle();
 assert.equal(JSON.stringify(loaded.probe.state.plan),JSON.stringify(original));
 assert.equal(loaded.element('message').value,original.message);
 assert.equal(loaded.requests.some(([method])=>method==='POST'),false);
 assert.ok(loaded.requests.some(([,path])=>path===`/api/chat/${original.turn_id}/events?afterEventId=0`));
 loaded.element('message').value='After reload';await loaded.probe.sendChat(false);
 assert.equal(loaded.probe.state.plan.conversation_id,original.conversation_id);
 assert.notEqual(loaded.probe.state.plan.turn_id,original.turn_id);
});

test('lost admission and reload never submit automatically and block another turn until resolved',async()=>{
 const storage=new Map(),first=screen({storage,stored:null});await settle();await first.probe.sendChat(false);
 const plan=JSON.stringify(first.probe.state.plan);
 const loaded=screen({storage,stored:null});await settle();await settle();
 assert.equal(JSON.stringify(loaded.probe.state.plan),plan);
 assert.equal(loaded.element('send-chat').disabled,true);assert.equal(loaded.element('retry-chat').disabled,false);
 await loaded.probe.sendChat(false);assert.equal(loaded.requests.some(([method])=>method==='POST'),false);
 await loaded.probe.sendChat(true);assert.equal(JSON.stringify(loaded.probe.state.plan),plan);
 assert.equal(loaded.requests.filter(([method])=>method==='POST').length,1);
});

test('late stream/state from another recipient cannot alter the current conversation',async()=>{
 const config=()=>Response.json({chatSendAllowed:true,defaultSenderUserId:701,defaultRecipientUserId:801,
  chatRecipients:[{label:'one',senderUserId:701,recipientUserId:801},{label:'two',senderUserId:702,recipientUserId:802}]});
 const ui=screen({config,post:accepted});await settle();await ui.probe.sendChat(false);await settle();
 const oldContext=ui.probe.chatContext(),oldId=ui.probe.state.plan.conversation_id;
 ui.element('chat-recipient').value='1';ui.element('chat-recipient').onchange();
 ui.element('message').value='Different recipient';await ui.probe.sendChat(false);await settle();
 const second={...ui.probe.state.plan};assert.notEqual(second.conversation_id,oldId);
 ui.finish(0);ui.probe.record({id:999,event:'turn.finished',data:{status:'COMPLETED'}},oldContext);
 await settle();assert.equal(ui.probe.state.status,'PROCESSING');assert.ok(ui.probe.state.log.items.every(e=>e.id!==999));
 assert.equal(JSON.stringify(ui.probe.state.plan),JSON.stringify(second));
 assert.equal(JSON.parse(ui.storage.get('console-chat/synthetic')).recipientUserId,802);
});

test('another login scope, removed recipient, or altered cached binding never restores a conversation',async()=>{
 for(const fault of ['login','recipient','invalid']){
  const storage=new Map(),first=screen({storage,post:accepted});await settle();await first.probe.sendChat(false);
  if(fault==='invalid'){const plan=JSON.parse(storage.get('console-chat/synthetic'));plan.recipientContext={private:'must not restore'};storage.set('console-chat/synthetic',JSON.stringify(plan));}
  const loaded=screen({storage,scope:fault==='login'?'other':'synthetic',config:fault==='recipient'?()=>Response.json({chatSendAllowed:true,defaultSenderUserId:7,defaultRecipientUserId:99}):undefined});
  await settle();await settle();assert.equal(loaded.probe.state.plan,null);assert.equal(loaded.storage.size,0);
  assert.equal(loaded.requests.some(([,path])=>path.startsWith('/api/chat/')),false);
 }
});

test('cancellation, reconnect, and an in-flight admission keep the same owner and turn',async()=>{
 let finish;const ui=screen({post:()=>new Promise(resolve=>{finish=resolve;}),read:path=>Response.json(path.endsWith('/cancellation')?{httpStatus:200,body:{data:{status:'CANCELLED'}}}:{data:{status:'CANCELLED'}})});
 await settle();const sending=ui.probe.sendChat(false),plan={...ui.probe.state.plan};
 ui.probe.newChat();assert.equal(ui.probe.state.plan.turn_id,plan.turn_id);assert.equal(ui.element('new-chat').disabled,true);
 finish(accepted());await sending;await settle();await ui.element('cancel-chat').onclick();
 assert.equal(ui.probe.state.status,'CANCELLED');assert.equal(ui.probe.state.plan.turn_id,plan.turn_id);
 assert.ok(ui.requests.some(([method,path])=>method==='PUT'&&path===`/api/chat/${plan.turn_id}/cancellation`));
 const reconnected=ui.probe.connect();await settle();ui.finish(undefined,'CANCELLED');await reconnected;
 assert.equal(ui.probe.state.plan.conversation_id,plan.conversation_id);
});


for(const code of ['CONSOLE_CONVERSATION_UNCONFIRMED','CONSOLE_CONVERSATION_NOT_OWNED']){
 test(`creation rejection ${code} offers explicit new conversation without resending`,async()=>{
  let posts=0;
  const ui=screen({stored:null,post:()=>++posts===1?Response.json({detail:code},{status:409}):Response.json({httpStatus:202,body:{data:{status:'PROCESSING'}}})});
  await settle();await ui.probe.sendChat(false);
  const rejected=ui.probe.state.plan;
  assert.equal(ui.element('retry-chat').disabled,true);
  assert.equal(ui.element('send-chat').disabled,false);
  assert.equal(ui.element('send-chat').textContent,'새 대화로 다시 보내기');
  assert.match(ui.element('chat-note').textContent,/아직 전송되지/);
  assert.equal(ui.storage.size,0);
  assert.equal(ui.requests.filter(([method])=>method==='POST').length,1);
  assert.equal(ui.requests.some(([,path])=>path.includes('/events?')),false);
  await ui.probe.sendChat(true);assert.equal(posts,1);
  await ui.probe.sendChat(false);await settle();
  const fresh=ui.probe.state.plan;
  for(const key of ['request_id','conversation_id','turn_id'])assert.notEqual(fresh[key],rejected[key]);
  assert.equal(fresh.message,rejected.message);
  assert.equal(ui.requests.filter(([method])=>method==='POST').length,2);
  ui.finish();await settle();
 });
}

const quickConfig=()=>Response.json({chatSendAllowed:true,quickCheckConfigured:true,paidCallsEnabled:true,
  defaultSenderUserId:701,defaultRecipientUserId:801,chatRecipients:[{label:'Synthetic recipient',senderUserId:701,recipientUserId:801}]});

test('quick check requires a fresh consent click, sends only fixed binding, and cannot double submit',async()=>{
 let complete;
 const ui=screen({config:quickConfig,quickPost:body=>new Promise(resolve=>{complete=()=>resolve(Response.json({httpStatus:202,plan:{...body,message:'Server synthetic question'},body:{data:{status:'PROCESSING'}}}));})});
 await settle();assert.equal(ui.requests.some(([method])=>method==='POST'),false);
 await ui.element('quick-check').onclick();assert.equal(ui.requests.some(([method])=>method==='POST'),false);
 ui.element('message').value='SYNTHETIC_PRIVATE_DRAFT';ui.element('quick-check-consent').checked=true;ui.element('quick-check-consent').onchange();
 const first=ui.element('quick-check').onclick();await ui.element('quick-check').onclick();await ui.probe.sendChat(false);
 const posts=ui.requests.filter(([method])=>method==='POST');assert.equal(posts.length,1);
 assert.equal(posts[0][1],'/api/chat/quick-check');assert.equal(posts[0][2].confirmCost,true);
 assert.deepEqual(Object.keys(posts[0][2]).sort(),['confirmCost','conversation_id','recipientUserId','request_id','senderUserId','turn_id']);
 assert.ok(!JSON.stringify(posts).includes('SYNTHETIC_PRIVATE_DRAFT'));
 assert.equal(ui.element('message').value,'SYNTHETIC_PRIVATE_DRAFT');assert.equal(ui.element('quick-check-consent').checked,false);
 assert.equal(ui.element('new-chat').disabled,true);assert.equal(ui.element('quick-check').disabled,true);
 complete();await first;await settle();
 assert.equal(ui.probe.state.plan.message,'Server synthetic question');assert.equal(ui.probe.state.plan.quickCheck,true);
 assert.equal(ui.element('quick-check-note').textContent.includes('1건 성공'),false);
 ui.finish();await settle();await settle();assert.match(ui.element('quick-check-note').textContent,/이 질문 1건 성공/);
 assert.equal(ui.probe.state.images.loaded,0);assert.equal(ui.probe.state.images.failed,0);
 assert.equal(ui.element('message').value,'SYNTHETIC_PRIVATE_DRAFT');
});

test('quick check readiness cannot be bypassed with consent and never labels a demo success',async()=>{
 const ui=screen({config:()=>Response.json({chatSendAllowed:true,mode:'DEMO',quickCheckConfigured:false})});await settle();
 ui.element('quick-check-consent').checked=true;ui.element('quick-check-consent').onchange();await ui.element('quick-check').onclick();
 assert.equal(ui.element('quick-check').disabled,true);assert.equal(ui.requests.some(([method])=>method==='POST'),false);
 assert.match(ui.element('quick-check-note').textContent,/합성 수신자와 기존 BE 권한/);
});

test('lost quick check admission and reload use GET only; explicit retry preserves the original IDs and requires consent again',async()=>{
 const storage=new Map(),ui=screen({config:quickConfig,storage,stored:null});await settle();
 ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();
 const plan=JSON.parse(JSON.stringify(ui.probe.state.plan)),first=ui.requests.find(([method])=>method==='POST')[2];
 assert.equal(ui.requests.filter(([method])=>method==='POST').length,1);
 const loaded=screen({config:quickConfig,storage,stored:null});await settle();await settle();
 assert.equal(JSON.stringify(loaded.probe.state.plan),JSON.stringify(plan));assert.equal(loaded.requests.some(([method])=>method==='POST'),false);
 assert.equal(loaded.element('retry-chat').disabled,true);await loaded.probe.sendChat(true);assert.equal(loaded.requests.some(([method])=>method==='POST'),false);
 loaded.element('quick-check-consent').checked=true;loaded.element('quick-check-consent').onchange();await loaded.probe.sendChat(true);
 const retried=loaded.requests.filter(([method])=>method==='POST');assert.equal(retried.length,1);assert.equal(retried[0][1],'/api/chat/quick-check');assert.deepEqual(retried[0][2],first);
 assert.equal(loaded.element('quick-check-consent').checked,false);
});

test('lost quick check ACK recovers through GET and SSE, clearing the live failure while retaining its event without another POST',async()=>{
 const ui=screen({config:quickConfig,acceptance:502,stored:'PROCESSING'});await settle();
 ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();await settle();
 assert.equal(ui.probe.state.status,'PROCESSING');assert.equal(ui.probe.state.quickCheckFailure,null);
 assert.ok(!ui.element('quick-check-note').textContent.includes('HTTP 502'));
 assert.ok(!ui.element('chat-note').textContent.includes('HTTP 502'));
 assert.match(ui.element('quick-check-note').textContent,/저장 상태와 응답/);
 ui.finish();await settle();await settle();
 assert.equal(ui.probe.state.status,'COMPLETED');assert.match(ui.element('quick-check-note').textContent,/이 질문 1건 성공/);
 assert.equal(ui.requests.filter(([method])=>method==='POST').length,1);
 assert.equal(ui.probe.state.log.items.filter(event=>event.event==='console.error'&&event.data.httpStatus===502).length,1);
});

test('explicit quick check retry clears an earlier failure on confirmed admission and keeps the frozen request',async()=>{
 let posts=0;
 const ui=screen({config:quickConfig,stored:null,quickPost:()=>++posts===1?Response.json({}, {status:502}):accepted()});await settle();
 ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();
 assert.match(ui.element('quick-check-note').textContent,/HTTP 502/);const first=ui.requests.find(([method])=>method==='POST')[2];
 ui.element('quick-check-consent').checked=true;await ui.probe.sendChat(true);await settle();
 assert.equal(ui.probe.state.status,'PROCESSING');assert.equal(ui.probe.state.quickCheckFailure,null);
 assert.ok(!ui.element('quick-check-note').textContent.includes('HTTP 502'));
 assert.ok(!ui.element('chat-note').textContent.includes('HTTP 502'));
 const submitted=ui.requests.filter(([method])=>method==='POST');assert.equal(submitted.length,2);assert.deepEqual(submitted[1][2],first);
 ui.finish();await settle();await settle();
 assert.match(ui.element('quick-check-note').textContent,/이 질문 1건 성공/);
 assert.equal(ui.probe.state.log.items.filter(event=>event.event==='console.error'&&event.data.httpStatus===502).length,1);
});

for(const status of ['COMPLETED','FAILED','CANCELLED'])test(`observed quick check terminal ${status} replaces the transport failure, keeping history`,async()=>{
 const ui=screen({config:quickConfig,stored:null,acceptance:502});await settle();ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();
 assert.match(ui.element('quick-check-note').textContent,/HTTP 502/);
 ui.probe.record({id:1,event:'turn.finished',data:{status}});
 assert.equal(ui.probe.state.quickCheckFailure,null);assert.ok(!ui.element('quick-check-note').textContent.includes('HTTP 502'));
 assert.ok(!ui.element('chat-note').textContent.includes('HTTP 502'));
 assert.equal(ui.probe.state.status,status);assert.equal(ui.probe.state.log.items.filter(event=>event.event==='console.error').length,1);
 assert.equal(ui.requests.filter(([method])=>method==='POST').length,1);
});

for(const code of ['CONSOLE_CONVERSATION_UNCONFIRMED','CONSOLE_CONVERSATION_NOT_OWNED'])test(`quick check ${code} retries only through fresh consent and IDs, never sending the normal draft`,async()=>{
 let posts=0;
 const ui=screen({config:quickConfig,stored:null,quickPost:()=>++posts===1?Response.json({detail:code},{status:409}):accepted()});await settle();
 ui.element('message').value='SYNTHETIC_PRIVATE_NORMAL_DRAFT';ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();
 const first=ui.requests.find(([method])=>method==='POST')[2];
 assert.equal(ui.element('send-chat').textContent,'요청 보내기 ↗');assert.equal(ui.element('message').value,'SYNTHETIC_PRIVATE_NORMAL_DRAFT');
 assert.match(ui.element('chat-note').textContent,/비용에 다시 동의한 뒤 빠른 점검을 다시 실행/);
 assert.match(ui.element('quick-check-note').textContent,/비용에 다시 동의한 뒤 빠른 점검을 다시 실행/);
 assert.ok(!ui.element('chat-note').textContent.includes('새 대화로 다시 보내기'));
 assert.equal(ui.element('quick-check-consent').checked,false);assert.equal(ui.element('retry-chat').disabled,true);assert.equal(ui.storage.size,0);
 await ui.probe.sendChat(true);await ui.element('quick-check').onclick();assert.equal(posts,1);
 ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();await settle();
 const sent=ui.requests.filter(([method])=>method==='POST');assert.equal(sent.length,2);assert.ok(sent.every(([,path])=>path==='/api/chat/quick-check'));
 for(const key of ['request_id','conversation_id','turn_id'])assert.notEqual(sent[1][2][key],first[key]);
 assert.ok(!JSON.stringify(sent).includes('SYNTHETIC_PRIVATE_NORMAL_DRAFT'));assert.equal(ui.element('message').value,'SYNTHETIC_PRIVATE_NORMAL_DRAFT');
 ui.finish();await settle();
});

test('normal chat after quick check starts another conversation and preserves the normal draft',async()=>{
 const ui=screen({config:quickConfig,quickPost:()=>accepted(),post:accepted,stored:'COMPLETED'});await settle();
 ui.element('message').value='A normal follow-up draft';ui.element('quick-check-consent').checked=true;await ui.element('quick-check').onclick();await settle();
 const quick={...ui.probe.state.plan};ui.finish();await settle();await settle();await ui.probe.sendChat(false);
 assert.notEqual(ui.probe.state.plan.conversation_id,quick.conversation_id);assert.equal(ui.probe.state.plan.quickCheck,undefined);
 assert.equal(ui.probe.state.plan.message,'A normal follow-up draft');assert.equal(ui.requests.filter(([method])=>method==='POST').at(-1)[1],'/api/chat');
});

test('late diagnostics for an old recipient cannot populate the new conversation',async()=>{
 let complete;
 const ui=screen({post:accepted,read:path=>path.endsWith('/diagnostics')?new Promise(resolve=>{complete=resolve;}):Response.json({data:{status:'PROCESSING'}})});
 await settle();await ui.probe.sendChat(false);await settle();
 const collecting=ui.probe.refreshDiagnostics();ui.probe.newChat();
 complete(Response.json({model:{name:'synthetic/old-model',status:'completed'},final:{status:'COMPLETED',requestId:'11111111-1111-4111-8111-111111111111'}}));await collecting;
 assert.equal(ui.probe.state.diagnostics,null);assert.equal(ui.element('processing-status').textContent,'미수집');
});

test('image diagnostics count browser image events only for the currently displayed cards',async()=>{
 const ui=screen();await settle();
 const image={tagName:'IMG',closest:()=>({getAttribute:()=> '101'})};
 ui.element('chat-products').contains=target=>target===image;
 ui.probe.observeImage({target:{tagName:'IMG'},type:'load'});assert.equal(ui.probe.state.images,undefined);
 ui.probe.observeImage({target:image,type:'load'});assert.equal(ui.probe.state.images.loaded,1);
 ui.probe.observeImage({target:image,type:'load'});assert.equal(ui.probe.state.images.loaded,1);
 ui.probe.observeImage({target:image,type:'error'});assert.equal(ui.probe.state.images.loaded,0);assert.equal(ui.probe.state.images.failed,1);
 ui.probe.newChat();assert.equal(ui.probe.state.images.loaded,0);assert.equal(ui.probe.state.images.failed,0);
});
