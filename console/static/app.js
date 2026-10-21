import {adminFetch, initializeAdmin} from './admin.js';
const adminSession=await initializeAdmin();
const chatStorageKey='console-chat/'+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(adminSession.csrf))),b=>b.toString(16).padStart(2,'0')).join('');
import {initializeProviderUsage} from './usage.js';
initializeProviderUsage();
import {createProductCards,createProductLoader} from './products.js';
import {renderOperations,tickOperations,acceptSnapshot} from './ops.js';
import {SSEParser,EventLog,redact,terminal,eventPreview,elapsedSince,elapsedLabel,HTTPHistory,httpTransport,httpBody,requestErrorMessage,parseProfileJSON,safeDiagnostics,diagnosticRows,chatResult} from './core.js';
/** @returns {any} */ const $=id=>document.getElementById(id);
/** @type {any} */ const state={log:new EventLog(),selected:null,plan:null,abort:null,round:0,started:null,ended:null,sending:false,profileSending:false,chatSendAllowed:false,profileSendAllowed:false,status:'준비',admitted:false,profile:null,profileBody:null,snapshot:null,page:'chat',lastProfileState:null,profileEpoch:0,chatEpoch:0,collectionFailed:null};
let profileTimer=null;
let unseen=0;const expanded=new WeakSet();
const httpHistory=new HTTPHistory(),httpOrigins=new Map(),httpOpen=new Set();
const loadProduct=createProductLoader(id=>api('/api/products/'+id));
function toast(message){$('toast').textContent=message;$('toast').classList.remove('hidden');setTimeout(()=>$('toast').classList.add('hidden'),4500);}
function beginHTTP(path,method='GET',body=null,stream=false){
  const family=path.startsWith('/api/chat')?'chat':path.startsWith('/api/profile')?'profile':null;if(!family)return null;
  const parts=path.split('?')[0].split('/'),key=family==='chat'?(body?.turn_id??parts[3]):body?.recipientUserId!==undefined?`${body.recipientUserId}:${body.sourceVersion}`:`${parts[3]}:${parts[4]}`;
  const group=family+'/'+key,started=performance.now();
  if(method==='POST'&&!httpOrigins.has(group)){if(httpOrigins.size>=256)httpOrigins.delete(httpOrigins.keys().next().value);httpOrigins.set(group,family==='chat'?state.started:started);}
  const query=path.includes('?afterEventId=')?{afterEventId:Number(path.split('?afterEventId=')[1])}:{};
  const item=httpHistory.add({family,group,method,path,query,requestBody:body,started,elapsedMs:elapsedSince(httpOrigins.get(group)??null,started),poll:method==='GET'&&!stream,stream,streamDurationMs:null,streamState:stream?'연결 중':null});renderHTTP();return item;
}
function responseHeaders(response){if(!response)return {};const result={};for(const name of ['content-type','retry-after','cache-control']){const value=response.headers.get(name);if(value!==null)result[name]=value;}return result;}
function finishHTTP(item,response,data,error=null){if(!item)return;httpHistory.complete(item,{relayStatus:response?.status??null,responseBody:data&&typeof data==='object'?Object.fromEntries(Object.entries(data).filter(([key])=>key!=='transport')):data,headers:responseHeaders(response),transport:data?.transport,durationMs:performance.now()-item.started,error});renderHTTP();}
function httpFailure(item){return Boolean(item.error||item.relayStatus>=400||item.transport.exchanges.some(e=>e.error||e.status>=400));}
function transportLabel(item){const t=item.transport;if(t.mode==='synthetic')return '내장 합성 · upstream 미사용';if(t.mode==='not-called')return 'upstream 미호출';if(!t.exchanges.length)return 'upstream 상태 미수신';return 'AI '+t.exchanges.map(e=>e.status??'미수신').join(' → ');}
function httpRow(item){
  const card=document.createElement('article');card.className='httprow'+(httpFailure(item)?' httpbad':'');card.dataset.key=String(item.id);
  const title=document.createElement('div');title.className='httptitle';const method=document.createElement('span'),path=document.createElement('span');method.className='httpmethod';method.textContent=item.method;path.className='httppath';path.textContent=item.path;title.append(method,path);
  const status=document.createElement('div');status.className='httpstatuses';status.textContent=`중계 ${item.relayStatus??(item.durationMs===null?'응답 대기':'상태 미수신')} · ${transportLabel(item)}`;
  const retry=item.transport.exchanges.map(e=>e.headers['retry-after']).filter(Boolean).at(-1)??item.headers['retry-after'];if(retry)status.textContent+=' · Retry-After '+retry;
  const code=item.transport.exchanges.map(e=>e.responseBody?.error?.code??e.error).filter(Boolean).at(-1)??item.error;if(code)status.textContent+=' · '+code;
  const binding=item.family==='chat'?'chat/'+item.group.split('/')[1]?.slice(0,8):item.group;
  const meta=document.createElement('div');meta.className='httpmeta';meta.textContent=`시작 +${elapsedLabel(item.elapsedMs)} · 소요 ${elapsedLabel(item.durationMs)}${item.stream?' (응답 헤더)':''} · ${binding}${item.stream?' · '+item.streamState:''}`;
  const details=document.createElement('details'),summary=document.createElement('summary'),raw=document.createElement('pre');summary.textContent='마스킹한 요청 / 응답 / 허용 헤더';raw.textContent=JSON.stringify({binding:item.group,query:item.query,request:item.requestBody,response:item.responseBody,relayHeaders:item.headers,upstream:item.transport,connectionError:item.error,streamDurationMs:item.streamDurationMs},null,2);details.open=httpOpen.has(item.id);details.ontoggle=()=>{if(!details.isConnected)return;if(details.open)httpOpen.add(item.id);else httpOpen.delete(item.id);};details.append(summary,raw);card.append(title,status,meta,details);return card;
}
function renderHTTP(){
  $('http-nav-count').textContent=String(httpHistory.items.length);if(state.page!=='network')return;
  const list=$('http-list'),scroll=list.scrollTop,anchor=Array.from(list.children).find(c=>c.getBoundingClientRect().bottom>list.getBoundingClientRect().top),key=anchor?.getAttribute('data-key'),offset=anchor?anchor.getBoundingClientRect().top-list.getBoundingClientRect().top:0;
  const liveKeys=new Set(httpHistory.items.flatMap(i=>[i.id,i.group+i.path]));for(const key of httpOpen)if(!liveKeys.has(key))httpOpen.delete(key);
  const items=httpHistory.items.filter(i=>($('http-family').value==='all'||i.family===$('http-family').value)&&(!$('http-errors').checked||httpFailure(i)));
  $('http-count').textContent=String(httpHistory.items.length);$('http-limit').textContent=`메모리 ${httpHistory.items.length}/1,000 · 오래된 기록 ${httpHistory.dropped}개 제외`;$('http-summary').textContent=`표시 ${items.length}개 · 오류 ${items.filter(httpFailure).length}개 · 상태 조회 ${items.filter(i=>i.poll).length}개`;
  const groups=new Map();for(const item of items)if(item.poll){const group=groups.get(item.group+item.path)??[];group.push(item);groups.set(item.group+item.path,group);}
  list.replaceChildren();const drawn=new Set();for(const item of items){
    if(item.poll&&!$('http-polls').checked){const id=item.group+item.path;if(drawn.has(id))continue;drawn.add(id);const group=groups.get(id),last=group.at(-1),fold=document.createElement('details'),summary=document.createElement('summary');fold.className='httppoll';fold.dataset.key=id;summary.textContent=`${item.family==='chat'?'채팅':'프로파일'} 상태 조회 ${group.length}회 · ${item.group} · 최근 중계 ${last.relayStatus??'미수신'} / ${transportLabel(last)}`;fold.open=httpOpen.has(id);fold.append(summary);if(fold.open)for(const row of group)fold.append(httpRow(row));fold.ontoggle=()=>{if(!fold.isConnected)return;if(fold.open&&!httpOpen.has(id)){httpOpen.add(id);renderHTTP();}else if(!fold.open)httpOpen.delete(id);};list.append(fold);
    }else list.append(httpRow(item));
  }
  if(!items.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='조건에 맞는 HTTP 기록이 없습니다.';list.append(empty);}
  const restored=Array.from(list.children).find(c=>c.getAttribute('data-key')===key);list.scrollTop=restored?scroll+restored.getBoundingClientRect().top-list.getBoundingClientRect().top-offset:scroll;
}
async function api(path,method='GET',body=null,options={}){
  const item=beginHTTP(path,method,body);let response=null,recorded=false;
  try{response=await adminFetch(path,{method,...(options.signal?{signal:options.signal}:{}),headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();finishHTTP(item,response,data);recorded=true;if(!response.ok)throw Object.assign(new Error(requestErrorMessage(response.status,data)),{httpStatus:response.status,conversationRestart:response.status===409&&['CONSOLE_CONVERSATION_NOT_OWNED','CONSOLE_CONVERSATION_UNCONFIRMED'].includes(data?.detail)});return data;
  }catch(e){if(!recorded){finishHTTP(item,response,null,response?'NON_JSON_RESPONSE':'CONNECTION_FAILED');if(e.name==='AbortError')throw e;throw new Error(response?'응답 JSON 형식 확인 필요':'연결 응답 없음');}throw e;}
}
function time(value){return value?new Date(value).toLocaleTimeString('ko-KR',{hour12:false}):'미수집';}
function chatControls(){
  const busy=state.sending||state.restoring;
  const unresolved=Boolean(state.plan&&(state.admitted||state.uncertain)&&!terminal(state.status));
  $('send-chat').disabled=busy||!state.chatSendAllowed||unresolved;
  $('retry-chat').disabled=busy||!state.plan||state.conversationRejected||Boolean(state.plan?.quickCheck&&!$('quick-check-consent').checked);
  $('send-chat').textContent=state.conversationRejected&&!state.plan?.quickCheck?'새 대화로 다시 보내기':'요청 보내기 ↗';
  $('new-chat').disabled=$('chat-recipient').disabled=Boolean(busy);
  $('cancel-chat').disabled=busy||!state.plan||!state.admitted||terminal(state.status);
  $('quick-check').disabled=busy||unresolved||!state.chatSendAllowed||!state.quickCheckConfigured||!$('quick-check-consent').checked;
  $('quick-check-consent').disabled=Boolean(busy);
  $('refresh-diagnostics').disabled=!state.plan||!state.admitted||Boolean(state.diagnosticsLoading);
}
function saveChat(){
  try{if(state.plan&&!state.conversationRejected)sessionStorage.setItem(chatStorageKey,JSON.stringify(state.plan));else sessionStorage.removeItem(chatStorageKey);}
  catch{if(!state.storageWarning){state.storageWarning=true;toast('브라우저 저장을 사용할 수 없어 새로고침 후 대화 복원이 제한됩니다.');}}
}
function savedChat(config){
  try{
    for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('console-chat/')&&key!==chatStorageKey)sessionStorage.removeItem(key);}
    const raw=sessionStorage.getItem(chatStorageKey);if(!raw)return null;if(raw.length>12000)throw Error();const plan=JSON.parse(raw);
    const uuid=value=>typeof value==='string'&&/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i.test(value);
    const fields=plan.quickCheck===true?'conversation_id,message,quickCheck,recipientUserId,request_id,senderUserId,turn_id':'conversation_id,message,recipientUserId,request_id,senderUserId,turn_id';
    if(Object.keys(plan).sort().join(',')!==fields||!['conversation_id','request_id','turn_id'].every(key=>uuid(plan[key]))||typeof plan.message!=='string'||!plan.message.length||plan.message.length>3000)throw Error();
    const choices=config.chatRecipients?.length?config.chatRecipients:[{senderUserId:config.defaultSenderUserId??7,recipientUserId:config.defaultRecipientUserId??42}];
    if(!choices.some(p=>p.senderUserId===plan.senderUserId&&p.recipientUserId===plan.recipientUserId))throw Error();
    return plan;
  }catch{try{sessionStorage.removeItem(chatStorageKey);}catch{}return null;}
}
function setStatus(value,reset=false){if(!reset&&terminal(state.status)&&value!==state.status)return;if(terminal(value)&&state.ended===null)state.ended=performance.now();if(terminal(value)&&state.started!==null)$('elapsed').textContent='경과 '+((state.ended-state.started)/1000).toFixed(1)+'s · 브라우저 관측';state.status=value;if(terminal(value))state.uncertain=false;$('chat-status').textContent=value;$('chat-status').className='status '+(value==='FAILED'?'bad':terminal(value)?'good':'busy');chatControls();}
function eventDetail(){const e=state.selected;if(!e)return;$('event-meta').textContent=`${e.event} · event ID ${e.id??'없음'}\n${elapsedLabel(e.elapsedMs)} 경과 · ${time(e.receivedAt)} 콘솔 수신 · 연결 ${e.round}회차`;$('event-json').textContent=JSON.stringify(['progress','answer.delta','cards','turn.finished'].includes(e.event)&&!e.data?.error?redact(e.data,$('reveal').checked):httpBody(e.data),null,2);$('event-preview-detail').textContent=eventPreview(e,$('reveal').checked).body;}
function eventMatches(event){const preview=eventPreview(event,$('reveal').checked),category=$('event-kind').value;return (category==='all'||preview.category===category)&&`${event.event} ${event.id} ${preview.title} ${preview.body}`.toLowerCase().includes($('event-filter').value.toLowerCase());}
function renderDiagnostics(){
  const list=$('processing-summary');list.replaceChildren();
  for(const [label,value] of diagnosticRows(state.diagnostics,state.images)){
    const row=document.createElement('div'),term=document.createElement('dt'),detail=document.createElement('dd');term.textContent=label;detail.textContent=value;row.append(term,detail);list.append(row);
  }
  $('processing-status').textContent=state.diagnosticsLoading?'수집 중':state.diagnostics?'관측된 항목만 표시':'미수집';
  chatControls();
}
async function refreshDiagnostics(context=chatContext()){
  if(!currentChat(context)||!state.admitted||state.diagnosticsLoading)return;
  state.diagnosticsLoading=true;renderDiagnostics();
  try{const value=await api('/api/chat/'+context.turnId+'/diagnostics');if(!currentChat(context))return;state.diagnostics=safeDiagnostics(value);$('diagnostic-note').textContent='기록된 관측만 표시합니다. 미수집은 실패나 0건을 뜻하지 않습니다.';}
  catch{if(currentChat(context)){$('diagnostic-note').textContent='처리 과정 수집을 확인하지 못했습니다. 마지막 관측이 있다면 그대로 표시합니다.';}}
  finally{if(currentChat(context)){state.diagnosticsLoading=false;renderDiagnostics();}}
}
function renderChatResult(){
  const result=chatResult(state.log.items);state.answer=result.text;
  $('chat-question').textContent=state.plan?String(redact(state.plan.message,true)):'';$('chat-question').classList.toggle('hidden',!state.plan);
  $('chat-answer').textContent=result.text||(result.products.length?'추천 상품을 확인해 주세요.':!state.plan?'메시지를 보내면 답변이 여기에 표시됩니다.':state.status==='FAILED'?'답변 생성을 완료하지 못했습니다.':state.status==='CANCELLED'?'작업이 취소되었습니다.':terminal(state.status)?'저장된 답변 본문은 미수집입니다.':'답변을 기다리고 있습니다.');
  const signature=JSON.stringify(result.products);
  if(signature!==state.productSignature){state.productSignature=signature;state.imageResults=new Map();state.images={loaded:0,failed:0};$('chat-products').replaceChildren();if(result.products.length)$('chat-products').append(createProductCards(result.products,{reveal:true,loadProduct}));renderDiagnostics();}
  $('result-scope').textContent=state.plan?.quickCheck?'빠른 점검 · 질문 1건':'현재 메시지';
  $('chat-result-note').textContent=state.plan?.quickCheck?'이 질문의 응답과 상품 사진만 관측합니다. 모델·상품 버전·소요 시간은 처리 과정에서 확인합니다.':'상품 사진은 추천 후 확인된 상품 정보에서 불러옵니다.';
  if(state.plan?.quickCheck)$('quick-check-note').textContent=state.quickCheckFailure??(state.status==='COMPLETED'?'이 질문 1건 성공 · 이미지 표시 결과는 처리 과정에서 따로 확인합니다.':state.status==='FAILED'?'이 질문의 응답 생성 실패 · 처리 과정에서 관측된 항목을 확인하세요.':state.status==='CANCELLED'?'이 질문의 작업이 취소되었습니다.':'빠른 점검의 저장 상태와 응답을 확인하고 있습니다.');
  else $('quick-check-note').textContent=state.quickCheckConfigured?'연결 설정이 있습니다. 클릭할 때 준비 상태와 권한을 확인합니다.':'실제 모델·상품 빠른 점검용 합성 수신자와 기존 BE 권한을 준비하세요.';
}
function archiveTurn(){
  if(!state.plan||!terminal(state.status)||state.plan.quickCheck)return;
  const result=chatResult(state.log.items);if(!result.text&&!result.products.length)return;
  const history=$('chat-history'),entry=document.createElement('details'),summary=document.createElement('summary'),answer=document.createElement('p');entry.className='past-turn';summary.textContent=String(redact(state.plan.message,true)).slice(0,160);answer.textContent=result.text||'상품 카드로 응답했습니다.';entry.append(summary,answer);const cards=$('chat-products').firstElementChild;if(cards)entry.append(cards);history.append(entry);while(history.children.length>10)history.firstElementChild.remove();
}
function observeImage(event){
  const target=event.target;if(target?.tagName!=='IMG'||!$('chat-products').contains(target))return;
  const id=target.closest('[data-product-id]')?.getAttribute('data-product-id');if(!id||!/^\d+$/.test(id))return;
  state.imageResults??=new Map();state.imageResults.set(id,event.type==='load'?'loaded':'failed');state.images={loaded:0,failed:0};for(const outcome of state.imageResults.values())state.images[outcome]++;renderDiagnostics();
}
function newEventHint(){const hint=$('new-events');hint.textContent=`새 이벤트 ${unseen}개 ↓`;hint.classList.toggle('hidden',unseen===0);}
function events(){
  const list=$('event-list'),scroll=list.scrollTop,atBottom=scroll+list.clientHeight>=list.scrollHeight-30;
  const anchor=Array.from(list.children).find(child=>child.getBoundingClientRect().bottom>list.getBoundingClientRect().top);
  const anchorKey=anchor?.getAttribute('data-key'),anchorOffset=anchor?anchor.getBoundingClientRect().top-list.getBoundingClientRect().top:0;
  list.replaceChildren();
  for(const event of state.log.items.filter(eventMatches)){
    const preview=eventPreview(event,$('reveal').checked),card=document.createElement('article');card.className='eventrow eventcard '+preview.category+(state.selected===event?' selected':'');card.setAttribute('data-key',String(state.log.items.indexOf(event)+state.log.dropped));
    const heading=document.createElement('button');heading.className='eventheading';heading.setAttribute('aria-label',`${preview.title} · ID ${event.id??'없음'} 상세`);
    const num=document.createElement('span'),type=document.createElement('span'),clock=document.createElement('span');num.className='eventnum';num.textContent='#'+(event.id??'—');type.className='eventtype';type.textContent=preview.title;clock.className='eventtime';clock.textContent=elapsedLabel(event.elapsedMs);heading.append(num,type,clock);
    const body=document.createElement('div');body.className='eventpreview';const full=expanded.has(event),length=full?Math.min(preview.body.length,2000):260;body.textContent=preview.body.slice(0,length);
    const meta=document.createElement('div');meta.className='eventsub';meta.textContent=event.event+' · 연결 '+event.round+'회차';
    card.append(heading,body);
    card.append(meta);card.onclick=()=>{state.selected=event;events();eventDetail();};
    if(preview.body.length>260){const more=document.createElement('button');more.className='preview-more';more.textContent=full?'접기':'더 보기';if(full&&preview.body.length>2000)more.textContent='접기 · 전체 내용은 상세에서';more.onclick=e=>{e.stopPropagation();if(full)expanded.delete(event);else expanded.add(event);events();};card.append(more);}
    list.append(card);
  }
  if(!list.children.length){const empty=document.createElement('div');empty.className='empty';empty.textContent=state.log.items.length?'필터에 맞는 이벤트가 없습니다.':'요청을 보내면 이벤트 내용이 여기에 표시됩니다.';list.append(empty);}
  $('event-count').textContent=String(state.log.items.length);$('error-count').textContent='오류 '+state.log.items.filter(e=>eventPreview(e).error).length;
  if($('autoscroll').checked&&atBottom){list.scrollTop=list.scrollHeight;unseen=0;}
  else {const restored=Array.from(list.children).find(child=>child.getAttribute('data-key')===anchorKey);list.scrollTop=restored?scroll+restored.getBoundingClientRect().top-list.getBoundingClientRect().top-anchorOffset:scroll;}
  newEventHint();
}
function chatContext(){return state.plan?{epoch:state.chatEpoch,turnId:state.plan.turn_id}:null;}
function currentChat(context){return Boolean(context&&context.epoch===state.chatEpoch&&context.turnId===state.plan?.turn_id);}
function record(event,context=chatContext(),round=state.round){if(!currentChat(context))return;const e={...event,receivedAt:new Date().toISOString(),elapsedMs:elapsedSince(state.started,performance.now()),round};if(state.log.add(e)){if((event.event==='turn.finished'||event.event==='console.cancellation')&&terminal(event.data?.status)){state.quickCheckFailure=null;setStatus(event.data.status);$('chat-note').textContent='저장된 최종 상태를 수신했습니다.';}if(!state.selected)state.selected=e;unseen++;events();eventDetail();renderChatResult();if(terminal(event.data?.status))void refreshDiagnostics(context);}}
async function reconcile(context=chatContext()){
  if(!currentChat(context))return false;
  try{const value=await api('/api/chat/'+context.turnId);if(!currentChat(context))return;
    let status=value.data?.status??'알 수 없음';const found=['PROCESSING','COMPLETED','CANCELLED','FAILED'].includes(status);if(found){state.admitted=true;state.quickCheckFailure=null;}if(status==='PROCESSING'&&!terminal(state.status))state.ended=null;setStatus(status);status=state.status;$('chat-note').textContent=terminal(status)?'저장된 최종 상태로 확인했습니다.':'연결 상태와 서버 작업 상태는 다릅니다. 저장 상태: '+status;renderChatResult();if(terminal(status))void refreshDiagnostics(context);return found;
  }catch(e){if(currentChat(context))$('chat-note').textContent='상태 수집 실패 · '+e.message;return false;}
}
async function connect(){
  const context=chatContext();if(!currentChat(context))return;if(state.abort)state.abort.abort();
  const controller=new AbortController();state.abort=controller;const round=++state.round;
  const currentStream=()=>currentChat(context)&&state.abort===controller;
  $('connection-state').textContent=`연결 ${round}회차 · 수신 중`;$('disconnect').disabled=false;$('reconnect').disabled=true;const parser=new SSEParser(),httpItem=beginHTTP(`/api/chat/${context.turnId}/events?afterEventId=${state.log.cursor}`,'GET',null,true);
  try{const response=await adminFetch(`/api/chat/${context.turnId}/events?afterEventId=${state.log.cursor}`,{signal:controller.signal});
    finishHTTP(httpItem,response,{stream:'SSE events recorded separately'});httpItem.streamState='수신 중';
    if(!currentStream()){await response.body?.cancel();return;}
    if(!response.ok)throw new Error('SSE HTTP '+response.status);const reader=response.body.getReader(),decoder=new TextDecoder();
    while(true){const {done,value}=await reader.read();if(!currentStream()){await reader.cancel();return;}if(done)break;for(const e of parser.push(decoder.decode(value,{stream:true}))){if(e.event==='console.http'){httpItem.transport=httpTransport(e.data);renderHTTP();}else record(e,context,round);}}
    for(const e of parser.push(decoder.decode())){if(e.event==='console.http'){httpItem.transport=httpTransport(e.data);renderHTTP();}else record(e,context,round);}
    httpItem.streamDurationMs=performance.now()-httpItem.started;httpItem.streamState='스트림 종료 · 작업 상태 별도';
    if(currentStream()){$('connection-state').textContent='스트림 종료 · 저장 상태 확인';await reconcile(context);}
  }catch(e){httpItem.streamDurationMs=performance.now()-httpItem.started;if(httpItem.relayStatus===null)finishHTTP(httpItem,null,null,e.name==='AbortError'?'CLIENT_DISCONNECTED':'CONNECTION_FAILED');httpItem.streamState=e.name==='AbortError'?'연결 끊김 · 작업 상태 별도':'연결 오류';if(currentStream()){$('connection-state').textContent=e.name==='AbortError'?'연결 끊김 · 작업은 유지':'연결 오류';if(e.name!=='AbortError')record({id:null,event:'console.error',data:{code:e.message}},context,round);await reconcile(context);}}
  finally{if(httpItem.streamDurationMs===null)httpItem.streamDurationMs=performance.now()-httpItem.started;if(httpItem.streamState==='수신 중')httpItem.streamState='스트림 종료 · 작업 상태 별도';renderHTTP();if(currentStream()){state.abort=null;$('disconnect').disabled=true;$('reconnect').disabled=false;}}
}
function resetTurn(){state.chatEpoch++;if(state.abort)state.abort.abort();state.abort=null;state.plan=null;state.admitted=false;state.uncertain=false;state.conversationRejected=false;state.log=new EventLog();unseen=0;state.selected=null;state.round=0;state.started=null;state.ended=null;state.diagnostics=null;state.diagnosticsLoading=false;state.quickCheckFailure=null;state.productSignature=null;state.images={loaded:0,failed:0};$('elapsed').textContent='경과 —';setStatus('준비',true);$('ids').textContent='메시지를 보내면 대화를 시작합니다.';$('event-json').textContent='이벤트 대기';$('event-preview-detail').textContent='이벤트를 선택하면 안전한 내용 요약이 표시됩니다.';$('event-meta').textContent='이벤트를 선택하세요.';$('retry-chat').disabled=true;$('reconnect').disabled=true;$('disconnect').disabled=true;events();renderChatResult();renderDiagnostics();}
function newChat(){if(state.sending||state.restoring)return;resetTurn();state.conversationId=null;$('chat-history').replaceChildren();$('message').value='';$('chat-note').textContent='새 대화입니다. 같은 수신자의 다음 메시지는 이 대화에서 이어집니다.';saveChat();}
async function restoreChat(plan){
  resetTurn();state.conversationId=plan.conversation_id;state.plan=plan;state.uncertain=true;state.restoring=true;if(!plan.quickCheck)$('message').value=plan.message;renderChatResult();
  $('ids').textContent=plan.conversation_id+'\n'+plan.turn_id;setStatus('저장 상태 확인 중',true);
  const context=chatContext();
  try{if(await reconcile(context))connect();}
  finally{if(currentChat(context)){state.restoring=false;chatControls();}}
}
async function sendChat(retry=false,quickCheck=false){
  if(state.sending||state.restoring||retry&&(!state.plan||state.conversationRejected))return;
  if((quickCheck||retry&&state.plan?.quickCheck)&&!$('quick-check-consent').checked)return;
  if(!retry){if(!state.chatSendAllowed||quickCheck&&!state.quickCheckConfigured||state.plan&&(state.admitted||state.uncertain)&&!terminal(state.status))return;
    if(quickCheck||state.plan?.quickCheck){state.conversationId=null;$('chat-history').replaceChildren();}else archiveTurn();
    resetTurn();state.conversationId??=crypto.randomUUID();state.plan={request_id:crypto.randomUUID(),conversation_id:state.conversationId,turn_id:crypto.randomUUID(),message:quickCheck?'빠른 점검용 합성 질문 · 서버에서 생성':$('message').value,recipientUserId:state.liveRecipient??42,senderUserId:state.liveSender??7,...(quickCheck?{quickCheck:true}:{})};}
  const context=chatContext();if(!currentChat(context))return;
  $('ids').textContent=state.plan.conversation_id+'\n'+state.plan.turn_id;state.sending=true;state.uncertain=true;if(state.plan.quickCheck)$('quick-check-consent').checked=false;saveChat();renderChatResult();chatControls();
  let upstreamStatus=null;
  try{if(!retry)state.started=performance.now();if(!terminal(state.status))state.ended=null;const {quickCheck:check,message,...binding}=state.plan;const reply=await api(check?'/api/chat/quick-check':'/api/chat','POST',check?{...binding,confirmCost:true}:state.plan);upstreamStatus=reply.httpStatus;if(!currentChat(context))return;
    if(check&&reply.plan&&Object.keys(binding).every(key=>reply.plan[key]===binding[key])&&typeof reply.plan.message==='string'&&reply.plan.message.length<=3000){state.plan.message=reply.plan.message;saveChat();}
    record({id:null,event:'console.accepted',data:{httpStatus:reply.httpStatus,requestId:reply.requestId}},context);if(![200,202].includes(reply.httpStatus))throw Object.assign(new Error(requestErrorMessage(reply.httpStatus,reply.body)),{httpStatus:reply.httpStatus});state.admitted=true;state.uncertain=false;state.quickCheckFailure=null;setStatus('PROCESSING');$('chat-note').textContent=terminal(state.status)?'저장된 최종 상태로 확인했습니다.':'요청 접수를 확인했습니다. 답변과 저장 상태를 확인합니다.';renderChatResult();connect();
  }catch(e){if(currentChat(context)){
    upstreamStatus=e.httpStatus??upstreamStatus;state.uncertain=upstreamStatus===null||upstreamStatus===408||upstreamStatus>=500;
    if(state.plan.quickCheck&&!terminal(state.status))state.quickCheckFailure=e.message;
    if(e.conversationRestart){state.conversationRejected=true;state.conversationId=null;state.admitted=false;state.uncertain=false;saveChat();}
    const quickRestart=state.conversationRejected&&state.plan.quickCheck;
    if(quickRestart)state.quickCheckFailure='합성 점검 질문은 아직 전송되지 않았습니다. 비용에 다시 동의한 뒤 빠른 점검을 다시 실행하세요.';
    if(state.ended===null)state.ended=performance.now();$('elapsed').textContent='경과 '+((state.ended-state.started)/1000).toFixed(1)+'s · 브라우저 관측';toast(e.message);setStatus('접수 확인 실패'+(upstreamStatus?' · HTTP '+upstreamStatus:''));
    $('chat-note').textContent=quickRestart?state.quickCheckFailure:e.message+(state.conversationRejected?' 새 대화로 다시 보내기를 눌러 주세요.':' · 동일 요청 재전송으로 재확인하세요. 서버 작업 상태는 별도입니다.');
    record({id:null,event:'console.error',data:{code:e.message,httpStatus:upstreamStatus}},context);if((state.admitted||state.uncertain)&&await reconcile(context))connect();
  }}
  finally{if(currentChat(context)){state.sending=false;chatControls();}}
}
$('send-chat').onclick=()=>sendChat(false);$('retry-chat').onclick=()=>sendChat(true);$('new-chat').onclick=newChat;$('scenario').onchange=()=>{$('message').value=$('scenario').value;};$('event-filter').oninput=events;$('reveal').onchange=()=>{events();eventDetail();};$('event-kind').onchange=events;$('error-count').onclick=()=>{$('event-kind').value='error';events();};$('new-events').onclick=()=>{$('event-list').scrollTop=$('event-list').scrollHeight;unseen=0;newEventHint();};$('event-list').onscroll=()=>{const l=$('event-list');if(l.scrollTop+l.clientHeight>=l.scrollHeight-30){unseen=0;newEventHint();}};$('disconnect').onclick=()=>{if(state.abort)state.abort.abort();};$('reconnect').onclick=connect;$('cancel-chat').onclick=async()=>{
  const context=chatContext();if(!currentChat(context))return;$('cancel-chat').disabled=true;
  try{const r=await api('/api/chat/'+context.turnId+'/cancellation','PUT',{});if(!currentChat(context))return;
    record({id:null,event:'console.cancellation',data:{httpStatus:r.httpStatus,status:r.body?.data?.status}},context);await reconcile(context);
  }catch(e){if(currentChat(context)){toast(e.message);$('cancel-chat').disabled=terminal(state.status);}}
};
$('quick-check').onclick=()=>sendChat(false,true);$('quick-check-consent').onchange=chatControls;
$('chat-products').addEventListener('load',observeImage,true);$('chat-products').addEventListener('error',observeImage,true);
$('refresh-diagnostics').onclick=()=>refreshDiagnostics();
async function profileState(){
  if(!state.profile)return;const epoch=state.profileEpoch,key={...state.profile},sequence=state.profileQuerySeq=(state.profileQuerySeq??0)+1;
  try{const r=await api(`/api/profile/${key.recipientUserId}/${key.sourceVersion}`);if(epoch!==state.profileEpoch||sequence<(state.profileAppliedSeq??0))return;state.profileAppliedSeq=sequence;if(['DELIVERED','FAILED','SUPERSEDED'].includes(state.lastProfileState)&&r.data?.status!==state.lastProfileState)return;
    $('profile-source').textContent=r.source+' · '+time(r.collectedAt);const row=r.data;$('profile-status').textContent=row?.status??'미수집 / 아직 저장 행 없음';$('profile-attempts').textContent=row?.callbackAttempts??'미수집';
    if(row&&row.status!==state.lastProfileState){state.lastProfileState=row.status;const item=document.createElement('div');item.textContent=time(r.collectedAt)+' 수집 · '+row.status;$('profile-history').prepend(item);}
    if(row&&['DELIVERED','FAILED','SUPERSEDED'].includes(row.status)){clearInterval(profileTimer);profileTimer=null;}$('profile-json').textContent=JSON.stringify(redact(r),null,2);
  }catch(e){if(epoch!==state.profileEpoch)return;$('profile-status').textContent='수집 실패';$('profile-source').textContent=e.message;}
}
$('send-profile').onclick=async()=>{
  if($('send-profile').disabled)return;
  try{const body={recipientUserId:Number($('recipient').value),sourceVersion:Number($('version').value),giftPreference:$('preference').value||null,dislikedCategories:parseProfileJSON($('dislikes').value,'비선호 카테고리'),reviews:parseProfileJSON($('reviews').value,'리뷰')};
    if(state.profile&&state.profile.recipientUserId===body.recipientUserId&&state.profile.sourceVersion===body.sourceVersion&&JSON.stringify(state.profileBody)!==JSON.stringify(body))throw new Error('같은 버전에는 같은 본문을 사용하세요. 새 시험은 버전을 올리세요.');
    const epoch=++state.profileEpoch;clearInterval(profileTimer);profileTimer=null;state.profile=null;state.profileBody=null;state.lastProfileState=null;
    $('profile-history').replaceChildren();$('profile-attempts').textContent='미수집';$('profile-http').textContent='접수 중';$('profile-status').textContent='미접수';$('profile-source').textContent=`시험 수신자 ${body.recipientUserId} / 버전 ${body.sourceVersion} · 접수 대기`;$('profile-json').textContent='새 요청 접수 중';$('refresh-profile').disabled=true;
    state.profileSending=true;$('send-profile').disabled=true;const r=await api('/api/profile','POST',body);if(epoch!==state.profileEpoch)return;
    $('profile-http').textContent=String(r.httpStatus);$('profile-json').textContent=JSON.stringify(redact(r),null,2);$('profile-note').textContent=r.httpStatus===202?'접수됨 · 아직 결과 생성/전달 완료가 아닙니다.':requestErrorMessage(r.httpStatus,r.body);
    if(r.httpStatus===202){state.profile=body;state.profileBody=body;$('refresh-profile').disabled=false;await profileState();if(epoch===state.profileEpoch&&!['DELIVERED','FAILED','SUPERSEDED'].includes(state.lastProfileState))profileTimer=setInterval(profileState,1000);}
    else $('profile-status').textContent='접수 거절';
  }catch(e){toast(e.message);$('profile-note').textContent=e.message;if(!state.profile){$('profile-http').textContent='접수 실패';$('profile-status').textContent='미접수';}}
  finally{state.profileSending=false;$('send-profile').disabled=!state.profileSendAllowed;}
};$('refresh-profile').onclick=profileState;
function counts(value){return value===null||value===undefined?'미수집':Object.entries(value).length?Object.entries(value).map(([key,n])=>`${key} ${n}`).join(' · '):'정상 수집 · 0건';}
function dashboardStale(){return Boolean(state.collectionFailed||state.snapshot&&Date.now()-Date.parse(state.snapshot.collectedAt)>15000);}
function renderDashboard(){renderOperations(state.snapshot,{loading:Boolean(state.dashboardLoading),error:state.collectionFailed||(dashboardStale()?'15초 이상 갱신되지 않은 관측':null),receivedAt:state.snapshotReceivedAt??performance.now(),httpItems:httpHistory.items});}
async function refreshDashboard(){if(state.dashboardLoading)return;state.dashboardLoading=true;const epoch=state.dashboardEpoch=(state.dashboardEpoch??0)+1;renderDashboard();const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
 try{const next=await api('/api/snapshot','GET',null,{signal:controller.signal});if(epoch!==state.dashboardEpoch)return;const accepted=acceptSnapshot(state.snapshot,next);if(!accepted.ok)throw new Error(accepted.reason);state.snapshot=next;state.snapshotReceivedAt=performance.now();state.collectionFailed=null;}
 catch(e){if(epoch===state.dashboardEpoch){state.collectionFailed=e.name==='AbortError'?'수집 응답 제한시간 초과':e.message;}}
 finally{clearTimeout(timer);if(epoch===state.dashboardEpoch){state.dashboardLoading=false;renderDashboard();}}}
$('refresh-dashboard').onclick=refreshDashboard;$('job-filter').onchange=renderDashboard;$('job-query').oninput=renderDashboard;
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>{state.page=button.getAttribute('data-page');document.querySelectorAll('.page').forEach(p=>p.classList.toggle('hidden',p.id!==state.page));document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n===button));if(state.page==='dashboard'){renderDashboard();refreshDashboard();}if(state.page==='network')renderHTTP();}));
setInterval(()=>{if(state.started!==null)$('elapsed').textContent='경과 '+(((state.ended??performance.now())-state.started)/1000).toFixed(1)+'s · 브라우저 관측';if(state.page==='dashboard'){const stale=dashboardStale();tickOperations(stale);if(stale&&!$('dashboard').classList.contains('is-stale'))renderDashboard();}},100);
setInterval(()=>{if(state.page==='dashboard')refreshDashboard();if(state.page==='network')renderHTTP();},5000);
if(new URLSearchParams(location.search).get('view')==='dashboard')document.querySelector('[data-page="dashboard"]').click();
let configEpoch=0;
function chooseChatRecipient(){
  const recipient=state.chatRecipients?.[Number($('chat-recipient').value)];
  if(recipient){
    if(state.sending||state.restoring){$('chat-recipient').value=String(state.chatRecipients.findIndex(p=>p.recipientUserId===state.liveRecipient&&p.senderUserId===state.liveSender));return;}
    if(recipient.recipientUserId!==state.liveRecipient||recipient.senderUserId!==state.liveSender)newChat();
    state.liveRecipient=recipient.recipientUserId;state.liveSender=recipient.senderUserId;
  }
}
$('chat-recipient').onchange=chooseChatRecipient;
async function refreshConfig(){
  const epoch=++configEpoch;
  state.chatSendAllowed=state.profileSendAllowed=false;state.quickCheckConfigured=false;
  $('send-chat').disabled=$('send-profile').disabled=true;chatControls();
  try{
    const r=await api('/api/config');if(epoch!==configEpoch)return;
    $('target').textContent=r.targetLabel;
    $('mode').textContent=r.paidCallsEnabled?'LIVE · OpenRouter':r.mode==='DEMO'?'FREE · 합성':'LOCAL TARGET';
    if(state.liveRecipient===undefined){state.liveRecipient=r.defaultRecipientUserId??42;state.liveSender=r.defaultSenderUserId??7;}
    let restore=null;if(!state.restoreChecked){state.restoreChecked=true;restore=savedChat(r);if(restore){state.liveRecipient=restore.recipientUserId;state.liveSender=restore.senderUserId;}}
    if(r.paidCallsEnabled){
      // A settings refresh must preserve the operator's draft and current turn.
      if(!state.liveInitialized){state.liveInitialized=true;$('recipient').value=String(state.liveRecipient);if(!restore)$('message').value='3만원 이하로 이 친구에게 어울리는 실용적인 선물 두 개를 추천해 주세요.';}
      $('scenario').disabled=true;
    }
    state.chatRecipients=r.chatRecipients??[];
    $('chat-recipient-field').classList.toggle('hidden',!state.chatRecipients.length);
    $('chat-recipient-summary').classList.toggle('hidden',Boolean(state.chatRecipients.length));
    $('chat-recipient-summary').textContent=`설정된 수신자 ${state.liveRecipient} · 보내는 사용자 ${state.liveSender}`;
    $('chat-recipient').replaceChildren();
    state.chatRecipients.forEach((recipient,index)=>{const option=document.createElement('option');option.value=String(index);option.textContent=recipient.label;option.selected=recipient.recipientUserId===state.liveRecipient&&recipient.senderUserId===state.liveSender;$('chat-recipient').append(option);});
    if(state.chatRecipients.length){const selected=state.chatRecipients.findIndex(p=>p.recipientUserId===state.liveRecipient&&p.senderUserId===state.liveSender);$('chat-recipient').value=String(Math.max(0,selected));chooseChatRecipient();}
    state.chatSendAllowed=r.chatSendAllowed===true;
    state.profileSendAllowed=r.profileSendAllowed===true;
    state.quickCheckConfigured=r.quickCheckConfigured===true;
    if(!state.plan?.quickCheck)$('quick-check-note').textContent=state.quickCheckConfigured?'연결 설정이 있습니다. 클릭할 때 준비 상태와 권한을 확인합니다.':'실제 모델·상품 빠른 점검용 합성 수신자와 기존 BE 권한을 준비하세요.';
    chatControls();
    $('send-profile').disabled=state.profileSending||!state.profileSendAllowed;
    if(!state.plan)$('chat-note').textContent=!state.chatSendAllowed?'Chat 키·서버 준비 상태 확인 필요 · 운영 대시보드에서 설정 저장 후 다시 확인합니다.':r.paidCallsEnabled?'실제 LLM · OpenRouter 키 사용 · 합성 입력만 사용':'합성 입력으로 요청·저장 상태를 확인합니다.';
    if(!state.profile&&!state.profileSending)$('profile-note').textContent=!state.profileSendAllowed?'모델 비용 정책 미확인 · 전송 차단':r.paidCallsEnabled?'실제 태깅/임베딩 · 콜백 수락과 실제 BE 저장은 별도 확인':'합성 입력으로 접수·콜백 상태를 확인합니다.';
    if(restore)await restoreChat(restore);
  }catch(e){if(epoch===configEpoch)toast(e.message);}
}
document.addEventListener('admin-settings-saved',refreshConfig);
refreshConfig();

$('http-family').onchange=renderHTTP;$('http-errors').onchange=renderHTTP;$('http-polls').onchange=renderHTTP;$('http-latest').onclick=()=>{$('http-list').scrollTop=$('http-list').scrollHeight;};
