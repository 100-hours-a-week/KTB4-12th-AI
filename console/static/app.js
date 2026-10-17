import {renderOperations,tickOperations,acceptSnapshot} from './ops.js';
import {SSEParser,EventLog,redact,terminal,eventPreview,elapsedSince,elapsedLabel,HTTPHistory,httpTransport,httpBody,requestErrorMessage,parseProfileJSON} from './core.js';
/** @returns {any} */ const $=id=>document.getElementById(id);
/** @type {any} */ const state={log:new EventLog(),selected:null,plan:null,abort:null,round:0,started:null,ended:null,sending:false,status:'준비',admitted:false,profile:null,profileBody:null,snapshot:null,page:'chat',lastProfileState:null,profileEpoch:0,chatEpoch:0,collectionFailed:null};
let profileTimer=null;
let unseen=0;const expanded=new WeakSet();
const httpHistory=new HTTPHistory(),httpOrigins=new Map(),httpOpen=new Set();
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
  try{response=await fetch(path,{method,...(options.signal?{signal:options.signal}:{}),headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();finishHTTP(item,response,data);recorded=true;if(!response.ok)throw Object.assign(new Error(requestErrorMessage(response.status,data)),{httpStatus:response.status});return data;
  }catch(e){if(!recorded){finishHTTP(item,response,null,response?'NON_JSON_RESPONSE':'CONNECTION_FAILED');if(e.name==='AbortError')throw e;throw new Error(response?'응답 JSON 형식 확인 필요':'연결 응답 없음');}throw e;}
}
function time(value){return value?new Date(value).toLocaleTimeString('ko-KR',{hour12:false}):'미수집';}
function setStatus(value,reset=false){if(!reset&&terminal(state.status)&&value!==state.status)return;if(terminal(value)&&state.ended===null)state.ended=performance.now();if(terminal(value)&&state.started!==null)$('elapsed').textContent='경과 '+((state.ended-state.started)/1000).toFixed(1)+'s · 브라우저 관측';state.status=value;$('chat-status').textContent=value;$('chat-status').className='status '+(value==='FAILED'?'bad':terminal(value)?'good':'busy');$('cancel-chat').disabled=!state.plan||!state.admitted||terminal(value);}
function eventDetail(){const e=state.selected;if(!e)return;$('event-meta').textContent=`${e.event} · event ID ${e.id??'없음'}\n${elapsedLabel(e.elapsedMs)} 경과 · ${time(e.receivedAt)} 콘솔 수신 · 연결 ${e.round}회차`;$('event-json').textContent=JSON.stringify(['progress','answer.delta','cards','turn.finished'].includes(e.event)&&!e.data?.error?redact(e.data,$('reveal').checked):httpBody(e.data),null,2);$('event-preview-detail').textContent=eventPreview(e,$('reveal').checked).body;}
function eventMatches(event){const preview=eventPreview(event,$('reveal').checked),category=$('event-kind').value;return (category==='all'||preview.category===category)&&`${event.event} ${event.id} ${preview.title} ${preview.body}`.toLowerCase().includes($('event-filter').value.toLowerCase());}
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
    card.append(heading,body,meta);card.onclick=()=>{state.selected=event;events();eventDetail();};
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
function record(event,context=chatContext(),round=state.round){if(!currentChat(context))return;const e={...event,receivedAt:new Date().toISOString(),elapsedMs:elapsedSince(state.started,performance.now()),round};if(state.log.add(e)){if((event.event==='turn.finished'||event.event==='console.cancellation')&&terminal(event.data?.status))setStatus(event.data.status);if(!state.selected)state.selected=e;unseen++;events();eventDetail();}}
async function reconcile(context=chatContext()){
  if(!currentChat(context))return;
  try{const value=await api('/api/chat/'+context.turnId);if(!currentChat(context))return;
    let status=value.data?.status??'알 수 없음';if(['PROCESSING','COMPLETED','CANCELLED','FAILED'].includes(status))state.admitted=true;setStatus(status);status=state.status;$('chat-note').textContent=terminal(status)?'저장된 최종 상태로 확인했습니다.':'연결 상태와 서버 작업 상태는 다릅니다. 저장 상태: '+status;
  }catch(e){if(currentChat(context))$('chat-note').textContent='상태 수집 실패 · '+e.message;}
}
async function connect(){
  const context=chatContext();if(!currentChat(context))return;if(state.abort)state.abort.abort();
  const controller=new AbortController();state.abort=controller;const round=++state.round;
  const currentStream=()=>currentChat(context)&&state.abort===controller;
  $('connection-state').textContent=`연결 ${round}회차 · 수신 중`;$('disconnect').disabled=false;$('reconnect').disabled=true;const parser=new SSEParser(),httpItem=beginHTTP(`/api/chat/${context.turnId}/events?afterEventId=${state.log.cursor}`,'GET',null,true);
  try{const response=await fetch(`/api/chat/${context.turnId}/events?afterEventId=${state.log.cursor}`,{signal:controller.signal});
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
function newChat(){if(state.sending)return;state.chatEpoch++;if(state.abort)state.abort.abort();state.abort=null;state.plan=null;state.admitted=false;state.log=new EventLog();unseen=0;state.selected=null;state.round=0;state.started=null;state.ended=null;$('elapsed').textContent='경과 —';setStatus('준비',true);$('ids').textContent='새 시험을 시작하면 UUID가 생성됩니다.';$('event-json').textContent='이벤트 대기';$('event-preview-detail').textContent='이벤트를 선택하면 안전한 내용 요약이 표시됩니다.';$('event-meta').textContent='이벤트를 선택하세요.';$('retry-chat').disabled=true;$('reconnect').disabled=true;$('disconnect').disabled=true;events();}
async function sendChat(retry=false){
  if(state.sending)return;if(!retry){newChat();state.plan={request_id:crypto.randomUUID(),conversation_id:crypto.randomUUID(),turn_id:crypto.randomUUID(),message:$('message').value,...(state.liveRecipient?{recipientUserId:state.liveRecipient}:{})};}
  const context=chatContext();if(!currentChat(context))return;
  $('ids').textContent=state.plan.conversation_id+'\n'+state.plan.turn_id;state.sending=true;$('send-chat').disabled=true;$('retry-chat').disabled=true;
  let upstreamStatus=null;
  try{if(!retry)state.started=performance.now();if(!terminal(state.status))state.ended=null;const reply=await api('/api/chat','POST',state.plan);upstreamStatus=reply.httpStatus;if(!currentChat(context))return;
    record({id:null,event:'console.accepted',data:{httpStatus:reply.httpStatus,requestId:reply.requestId}},context);if(![200,202].includes(reply.httpStatus))throw Object.assign(new Error(requestErrorMessage(reply.httpStatus,reply.body)),{httpStatus:reply.httpStatus});state.admitted=true;setStatus('PROCESSING');connect();
  }catch(e){if(currentChat(context)){upstreamStatus=e.httpStatus??upstreamStatus;if(state.ended===null)state.ended=performance.now();$('elapsed').textContent='경과 '+((state.ended-state.started)/1000).toFixed(1)+'s · 브라우저 관측';toast(e.message);setStatus('접수 확인 실패'+(upstreamStatus?' · HTTP '+upstreamStatus:''));$('chat-note').textContent=e.message+' · 동일 요청 재전송으로 재확인하세요. 서버 작업 상태는 별도입니다.';record({id:null,event:'console.error',data:{code:e.message,httpStatus:upstreamStatus}},context);}}
  finally{if(currentChat(context)){state.sending=false;$('send-chat').disabled=false;$('retry-chat').disabled=false;}}
}
$('send-chat').onclick=()=>sendChat(false);$('retry-chat').onclick=()=>sendChat(true);$('new-chat').onclick=newChat;$('scenario').onchange=()=>{$('message').value=$('scenario').value;};$('event-filter').oninput=events;$('reveal').onchange=()=>{events();eventDetail();};$('event-kind').onchange=events;$('error-count').onclick=()=>{$('event-kind').value='error';events();};$('new-events').onclick=()=>{$('event-list').scrollTop=$('event-list').scrollHeight;unseen=0;newEventHint();};$('event-list').onscroll=()=>{const l=$('event-list');if(l.scrollTop+l.clientHeight>=l.scrollHeight-30){unseen=0;newEventHint();}};$('disconnect').onclick=()=>{if(state.abort)state.abort.abort();};$('reconnect').onclick=connect;$('cancel-chat').onclick=async()=>{
  const context=chatContext();if(!currentChat(context))return;$('cancel-chat').disabled=true;
  try{const r=await api('/api/chat/'+context.turnId+'/cancellation','PUT',{});if(!currentChat(context))return;
    record({id:null,event:'console.cancellation',data:{httpStatus:r.httpStatus,status:r.body?.data?.status}},context);await reconcile(context);
  }catch(e){if(currentChat(context)){toast(e.message);$('cancel-chat').disabled=terminal(state.status);}}
};
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
    $('send-profile').disabled=true;const r=await api('/api/profile','POST',body);if(epoch!==state.profileEpoch)return;
    $('profile-http').textContent=String(r.httpStatus);$('profile-json').textContent=JSON.stringify(redact(r),null,2);$('profile-note').textContent=r.httpStatus===202?'접수됨 · 아직 결과 생성/전달 완료가 아닙니다.':requestErrorMessage(r.httpStatus,r.body);
    if(r.httpStatus===202){state.profile=body;state.profileBody=body;$('refresh-profile').disabled=false;await profileState();if(epoch===state.profileEpoch&&!['DELIVERED','FAILED','SUPERSEDED'].includes(state.lastProfileState))profileTimer=setInterval(profileState,1000);}
    else $('profile-status').textContent='접수 거절';
  }catch(e){toast(e.message);$('profile-note').textContent=e.message;if(!state.profile){$('profile-http').textContent='접수 실패';$('profile-status').textContent='미접수';}}
  finally{$('send-profile').disabled=false;}
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
api('/api/config').then(r=>{$('target').textContent=r.targetLabel;$('mode').textContent=r.paidCallsEnabled?'LIVE · 비용 예약':r.mode==='DEMO'?'FREE · 합성':'LOCAL TARGET';if(r.paidCallsEnabled){state.liveRecipient=r.defaultRecipientUserId??42;$('recipient').value=String(state.liveRecipient);$('scenario').disabled=true;$('message').value='합성 검증: 3만원 이하의 실용적인 컵 선물 하나를 상품 검색으로 확인하고 추천해 주세요.';$('chat-note').textContent='실제 LLM · 중앙 누적 예산 선예약 · 합성 입력만 사용';$('profile-note').textContent='실제 태깅/임베딩 · 콜백 저장 후 다음 새 채팅에 수신자 스냅샷 적용';}if(!r.profileSendAllowed){$('send-profile').disabled=true;$('profile-note').textContent='모델 비용 정책 미확인 · 전송 차단';}}).catch(e=>toast(e.message));

$('http-family').onchange=renderHTTP;$('http-errors').onchange=renderHTTP;$('http-polls').onchange=renderHTTP;$('http-latest').onclick=()=>{$('http-list').scrollTop=$('http-list').scrollHeight;};
