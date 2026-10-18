'use strict';
const $ = id => document.getElementById(id);
const API = document.body.dataset.api;
const fixture = document.body.dataset.fixture === 'true';
const key = 'minimal-chat:' + API + ':' + fixture;
let state = null, authenticated = false, busy = false, controller = null, generation = 0;
const views = new Map();
try { state = JSON.parse(sessionStorage.getItem(key)); } catch {}
const save = () => state ? sessionStorage.setItem(key, JSON.stringify(state)) : sessionStorage.removeItem(key);
const terminal = t => ['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.status);
function node(tag, cls='', text='') { const e=document.createElement(tag);e.className=cls;e.textContent=text;return e; }
function message(text='') { $('status').textContent=text; }
function showLogin() { authenticated=false;$('login').hidden=false;$('chat').hidden=true;$('new-chat').hidden=true;setBusy(false); }
function showChat() { authenticated=true;$('login').hidden=true;$('chat').hidden=false;$('new-chat').hidden=false;setBusy(true); }
function setBusy(value) {
  busy=value;$('send').disabled=value;$('question').disabled=value;$('new-chat').disabled=value||!!state?.pending;
  $('cancel').hidden=!state?.pending;$('reconnect').hidden=!state?.pending || value;
}
async function checked(response) {
  if (response.ok) return response;
  let body;try { body=await response.json(); } catch {}
  if (response.status===401) showLogin();
  const error=new Error(body?.message || (response.status===429?'잠시 후 다시 시도해 주세요.':'연결을 확인한 뒤 다시 시도해 주세요.'));
  error.status=response.status;throw error;
}
async function api(path, method='GET', body) {
  const response=await fetch(API+path,{method,credentials:'same-origin',redirect:'error',
    ...(body===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  await checked(response);return response.json();
}
function render(t) {
  let view=views.get(t.turnId);
  if (!view) {
    view={root:node('article','turn'),user:node('div','user'),answer:node('div','answer'),progress:node('div','progress'),cards:node('div','cards'),error:node('div','turn-error')};
    view.root.append(view.user,view.answer,view.progress,view.cards,view.error);$('messages').append(view.root);views.set(t.turnId,view);
  }
  view.t=structuredClone(t);view.root.dataset.status=t.status;view.user.textContent=t.message;
  view.answer.textContent=(t.blocks||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n');
  view.answer.hidden=!view.answer.textContent;
  view.progress.textContent=t.status==='PROCESSING'?(t.progressMessage||'답변을 기다리고 있어요.'):(t.status==='CANCELLED'?'중지됨':'');
  view.error.textContent=t.error?.message||'';view.cards.replaceChildren();
  for (const p of (t.blocks||[]).flatMap(b=>b.type==='cards'?b.products:[])) {
    if (!Number.isSafeInteger(p.productId)||p.productId<1||typeof p.reason!=='string') continue;
    const card=node('div','card');card.append(node('strong','',fixture?'가상 상품 ID '+p.productId:'상품 ID '+p.productId),node('div','',p.reason));view.cards.append(card);
    // Product detail expansion belongs here after an authenticated product API + ID mapping is verified.
    // No product/name/price/image guesses and no additional product requests in this example.
  }
  return view;
}
function finish(t) { render(t);if(state?.pending?.tid===t.turnId){state.pending=null;save();}setBusy(false);message();$('question').focus(); }
async function snapshot() {
  const pending=state.pending;
  const value=await api('/conversations/'+state.cid+'/turns/'+pending.tid);
  if (value.data.turnId!==pending.tid) throw new Error('답변 연결을 확인해 주세요.');
  if(state?.pending?.tid===pending.tid)render(value.data);return value.data;
}
async function stream(t, version) {
  controller=new AbortController();let cursor=t.lastEventId,frame={data:[]},pending='';
  const response=await checked(await fetch(API+'/conversations/'+state.cid+'/turns/'+t.turnId+'/events?afterEventId='+cursor,
    {credentials:'same-origin',redirect:'error',headers:{Accept:'text/event-stream'},signal:controller.signal}));
  if (!response.headers.get('content-type')?.startsWith('text/event-stream')) throw new Error('답변을 다시 연결해 주세요.');
  const reader=response.body.getReader(),decoder=new TextDecoder();let ended=false;
  function line(raw) {
    if (!raw) {
      if(frame.data.length) {
        const id=Number(frame.id),data=JSON.parse(frame.data.join('\n'));
        if(!Number.isSafeInteger(id)||id<1) throw new Error('답변 번호를 확인해 주세요.');
        if(id>cursor) {
          if(id!==cursor+1)throw new Error('답변을 다시 연결해 주세요.');
          let next=structuredClone(views.get(t.turnId).t);
          if(frame.event==='turn.finished') {
            if(data.turnId!==t.turnId||data.lastEventId!==id||!terminal(data))throw new Error('답변 상태를 확인해 주세요.');
            next=data;ended=true;
          } else if(frame.event==='progress') next.progressMessage=data.message;
          else if(frame.event==='answer.delta') {
            delete next.progressMessage;const tail=next.blocks.at(-1);
            if(tail?.type==='text')tail.text+=data.delta;else next.blocks.push({type:'text',text:data.delta});
          } else if(frame.event==='cards') next.blocks.push({type:'cards',products:data.products});
          else throw new Error('답변 형식을 확인해 주세요.');
          next.lastEventId=id;cursor=id;render(next);
          if(ended)finish(next);
        }
      }
      frame={data:[]};return;
    }
    if(raw.startsWith(':'))return;
    const at=raw.indexOf(':'),name=at<0?raw:raw.slice(0,at);let value=at<0?'':raw.slice(at+1);if(value.startsWith(' '))value=value.slice(1);
    if(name==='data')frame.data.push(value);else if(name==='event'||name==='id')frame[name]=value;
  }
  try {
    while(version===generation) {
      const chunk=await reader.read();if(version!==generation)break;pending+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done});
      let at;while((at=pending.indexOf('\n'))>=0){let raw=pending.slice(0,at);pending=pending.slice(at+1);if(raw.endsWith('\r'))raw=raw.slice(0,-1);line(raw);}
      if(chunk.done||ended)break;
    }
    if(!ended&&version===generation)throw new Error('답변 연결이 끊겼습니다.');
  } finally { await reader.cancel().catch(()=>{}); }
}
async function follow() {
  const version=++generation;controller?.abort();setBusy(true);message();
  for(let attempt=0;attempt<3&&version===generation;attempt++) {
    try { const t=await snapshot();if(version!==generation)return;if(terminal(t)){finish(t);return;}await stream(t,version);return; }
    catch(error) {
      if(version!==generation||error.name==='AbortError')return;
      if(error.status===401){message(error.message);return;}
      if(error.status===404||attempt===2){setBusy(false);message(error.message+' 다시 연결 버튼으로 같은 질문을 확인할 수 있습니다.');return;}
      await new Promise(resolve=>setTimeout(resolve,500*(attempt+1)));
    }
  }
}
async function submitPending() {
  const original=state?.pending;if(!original)return;
  setBusy(true);message();
  try {
    await api('/conversations/'+state.cid,'PUT',{fixture});
    const p=original;
    const accepted=await api('/conversations/'+state.cid+'/turns/'+p.tid,'PUT',{message:p.message});
    if(state?.pending?.tid!==original.tid)return;
    render(accepted.data);if(terminal(accepted.data)){finish(accepted.data);return;}await follow();
  } catch(error) {
    if(state?.pending?.tid!==original.tid)return;
    if([400,429].includes(error.status)){
      state.pending=null;save();$('question').value=original.message;
      views.get(original.tid)?.root.remove();views.delete(original.tid);
    }
    setBusy(false);message(error.message+(state.pending?' 다시 연결하면 같은 질문의 접수 상태를 확인합니다.':''));
  }
}
$('login').onsubmit=async event=>{
  event.preventDefault();const code=$('code').value;$('code').value='';
  try {await api('/session','POST',{token:code});showChat();message();await restore();}
  catch(error){message(error.message);}
};
$('compose').onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  if(state?.pending){message('이전 질문을 다시 연결하거나 중지한 뒤 보내 주세요.');return;}
  const text=$('question').value.trim();if(!text||[...text].length>200)return;
  if(!state)state={cid:crypto.randomUUID(),pending:null};
  state.pending={tid:crypto.randomUUID(),message:text};save();$('question').value='';
  render({turnId:state.pending.tid,message:text,status:'PROCESSING',blocks:[],lastEventId:0});await submitPending();
};
$('reconnect').onclick=async()=>{
  if(!state?.pending||busy)return;
  setBusy(true);
  try {await api('/conversations/'+state.cid,'PUT',{fixture});await snapshot();await follow();}
  catch(error){if(error.status===404){await submitPending();}else{setBusy(false);message(error.message);}}
};
$('cancel').onclick=async()=>{
  if(!state?.pending)return;
  const tid=state.pending.tid,cid=state.cid;
  try {const data=await api('/conversations/'+cid+'/turns/'+tid+'/cancellation','PUT');if(state?.pending?.tid!==tid)return;++generation;controller?.abort();finish(data.data);}
  catch(error){message(error.message+' 다시 연결해 상태를 확인하세요.');}
};
$('new-chat').onclick=()=>{if(busy||state?.pending)return;++generation;controller?.abort();state=null;save();views.clear();$('messages').replaceChildren();message();setBusy(false);$('question').focus();};
async function restore() {
  if(!state){setBusy(false);return;}
  try {
    // This PUT reuses the original UUID/identity, restoring ownership after a lost ACK.
    const value=await api('/conversations/'+state.cid,'PUT',{fixture});
    for(const t of value.data.turns)render(t);
    const pending=value.data.turns.find(t=>t.status==='PROCESSING');
    if(pending){state.pending={tid:pending.turnId,message:pending.message};save();await follow();}
    else if(state.pending){const saved=value.data.turns.find(t=>t.turnId===state.pending.tid);if(saved)finish(saved);else{setBusy(false);message('질문 접수를 확인하려면 다시 연결하세요.');}}
    else setBusy(false);
  } catch(error) {
    if(error.status===409||error.status===404){state=null;save();views.clear();$('messages').replaceChildren();setBusy(false);message('새 접속에서 새 대화를 시작해 주세요.');}
    else{setBusy(false);message(error.message);}
  }
}
(async()=>{try{await api('/session');showChat();await restore();}catch(error){showLogin();if(error.status!==401)message(error.message);}})();
