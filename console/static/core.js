/** @typedef {{id:number|null,event:string,data:any,receivedAt:string,round:number,elapsedMs?:number|null}} EventItem */
export class SSEParser {
  constructor(){this.buffer='';}
  /** @param {string} chunk @returns {Array<{id:number|null,event:string,data:any}>} */
  push(chunk){
    this.buffer+=chunk;if(this.buffer.length>1048576)throw new Error('SSE frame limit exceeded');this.buffer=this.buffer.replace(/\r\n/g,'\n'); const out=[];
    while(this.buffer.includes('\n\n')){
      const index=this.buffer.indexOf('\n\n'),frame=this.buffer.slice(0,index);this.buffer=this.buffer.slice(index+2);
      let id=null,event='message';const data=[];
      for(const line of frame.split('\n')){if(line.startsWith('id: '))id=Number(line.slice(4));if(line.startsWith('event: '))event=line.slice(7);if(line.startsWith('data:'))data.push(line.slice(5).trimStart());}
      if(data.length){let value;try{value=JSON.parse(data.join('\n'));}catch{value={text:data.join('\n')};}out.push({id:Number.isSafeInteger(id)?id:null,event,data:value});}
    }return out;
  }
}
export class EventLog {
  constructor(limit=1000){this.limit=limit;/** @type {EventItem[]} */this.items=[];this.cursor=0;this.dropped=0;}
  /** @param {EventItem} event */
  add(event){if(event.id!==null&&event.id<=this.cursor)return false;if(event.id!==null)this.cursor=Math.max(this.cursor,event.id);this.items.push(event);if(this.items.length>this.limit){this.items.shift();this.dropped++;}return true;}
}
export function redact(value,reveal=false,key=''){
  if(/token|secret|password|authorization|api.?key|csrf|cookie|session|credential|hash/i.test(key))return '[토큰 마스킹]';
  if(/prompt|provider.*(response|body)|callback.*url|raw.?response/i.test(key))return '[민감 진단 숨김]';
  if(/^(code|errorcode)$/i.test(key))return diagnosticCode(value);
  if(/^detail$/i.test(key)&&typeof value==='string')return '[오류 상세 숨김]';
  if(key==='error'&&value&&typeof value==='object')return {code:diagnosticCode(value.code),...(value.traceId?{traceId:/^[0-9a-f-]{36}$/i.test(value.traceId)?value.traceId:'[숨김]'}:{}),...(value.message?{message:'[오류 상세 숨김]'}:{})};
  if(!reveal&&/^(delta|text|message|reason|giftPreference|reviews|preferredTags|dislikedTags|evidence)$/i.test(key))return '[원문 숨김]';
  if(Array.isArray(value))return value.map(v=>redact(v,reveal,key));
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,redact(v,reveal,k)]));
  return typeof value==='string'?value.replace(/Bearer\s+[^\s"']+|\b(?:sk-|hf_)[A-Za-z0-9_-]{12,}/g,'[토큰 마스킹]'):value;
}
export function terminal(status){return ['COMPLETED','CANCELLED','FAILED'].includes(status);}
export function elapsedSince(start,received){return start===null||!Number.isFinite(start)||!Number.isFinite(received)||received<start?null:received-start;}
export function elapsedLabel(milliseconds){return milliseconds===null||!Number.isFinite(milliseconds)?'기준 없음':(milliseconds/1000).toFixed(1)+'초';}

export function diagnosticCode(value){return typeof value==='string'&&/^(UNAUTHORIZED|FORBIDDEN|NOT_FOUND|CONFLICT|INVALID_REQUEST|PAYLOAD_TOO_LARGE|TOO_MANY_REQUESTS|SERVICE_UNAVAILABLE|INTERNAL_SERVER_ERROR|SNAPSHOT_MISMATCH|UPSTREAM_CONNECTION_FAILED|NON_JSON_RESPONSE|SYNTHETIC_FAILURE|SYNTHETIC_OVERLOAD|CONTRACT_7_7_REJECTED|CALLBACK_UNREACHABLE|CALLBACK_STALE|MODEL_ERROR|CONTRACT_7_6_UNKNOWN_FIELD|CONTRACT_7_9_SCHEMA|NO_ACTIVE_CATALOG|STORE_FAILED|PIPELINE_ERROR|invalid_answer|mixed_text_and_tools|invalid_tool_index|tool_payload_limit|incomplete_model_response|empty_model_response|one_tool_operation_required|missing_tool_id|unavailable_tool|tool_budget_exhausted|model_call_budget_exhausted|invalid_model_response|cost_exceeded_reservation)$/.test(value)?value:'미분류 오류';}
export function httpBody(value){
  let nodes=0;
  function visit(v,key='',depth=0){
    if(/cookie|authorization|token|secret|password|api.?key/i.test(key))return '[토큰 마스킹]';
    if(/prompt|provider.*(response|body)|callback.*url|raw.?response/i.test(key))return '[민감 진단 숨김]';
    if(/^detail$/i.test(key)&&typeof v==='string')return '[오류 상세 숨김]';
    if(/^(code|errorcode)$/i.test(key))return diagnosticCode(v);
    if(key==='error'&&v&&typeof v==='object')return redact(v,false,'error');
    if(/^(message|text|delta|reason|giftPreference|progressMessage|query|queryText|query_text|recipientContext|input|ctx|msg|reviews|dislikedCategories|preferredTags|dislikedTags|evidence)$/i.test(key))return '[원문 숨김]';
    if(++nodes>500||depth>8)return '[기록 제한]';
    if(Array.isArray(v))return v.slice(0,30).map(x=>visit(x,'',depth+1));
    if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).slice(0,30).map(([k,x])=>[k,visit(x,k,depth+1)]));
    return typeof v==='string'?String(redact(v,true)).slice(0,1500):v;
  }
  const safe=visit(value),serialized=JSON.stringify(safe);
  return serialized?.length>12000?{truncated:true,maskedPreview:serialized.slice(0,12000)}:safe;
}
export function httpHeaders(headers={}){const safe={};for(const [k,v] of Object.entries(headers))if(['content-type','retry-after','cache-control'].includes(k.toLowerCase()))safe[k.toLowerCase()]=String(redact(v,true)).slice(0,200);return safe;}
export function httpPath(path){return String(path).split('?')[0].replace(/[0-9a-f]{8}-[0-9a-f-]{27}/gi,'{id}').replace(/\/\d+(?=\/|$)/g,'/{id}');}
export function httpTransport(value){
  if(!value)return {mode:'unknown',exchanges:[]};
  return {mode:['synthetic','not-called','attempted'].includes(value.mode)?value.mode:'unknown',exchanges:(Array.isArray(value.exchanges)?value.exchanges:[]).slice(0,10).map(e=>({method:e.method,path:httpPath(e.path),status:Number.isInteger(e.status)&&e.status>=100&&e.status<=599?e.status:null,headers:httpHeaders(e.headers),requestBody:httpBody(e.requestBody),query:httpBody(e.query??{}),responseBody:httpBody(e.responseBody),durationMs:typeof e.durationMs==='number'&&e.durationMs>=0?e.durationMs:null,error:e.error?String(redact(e.error,true)).slice(0,120):null}))};
}
export class HTTPHistory {
  constructor(limit=1000){this.items=[];this.limit=limit;this.nextId=1;this.dropped=0;}
  add(entry){const item={id:this.nextId++,family:entry.family,group:entry.group,method:entry.method,path:httpPath(entry.path),query:httpBody(entry.query??{}),started:entry.started,elapsedMs:entry.elapsedMs,poll:Boolean(entry.poll),stream:Boolean(entry.stream),streamDurationMs:null,streamState:entry.streamState??null,requestBody:httpBody(entry.requestBody),responseBody:null,headers:{},transport:{mode:'unknown',exchanges:[]},relayStatus:null,durationMs:null,error:null};this.items.push(item);if(this.items.length>this.limit){this.items.shift();this.dropped++;}return item;}
  complete(item,fields){item.relayStatus=Number.isInteger(fields.relayStatus)&&fields.relayStatus>=100&&fields.relayStatus<=599?fields.relayStatus:null;item.durationMs=fields.durationMs??null;item.responseBody=httpBody(fields.responseBody);item.headers=httpHeaders(fields.headers);item.transport=httpTransport(fields.transport);item.error=fields.error?String(redact(fields.error,true)).slice(0,120):null;}
}

/** Previews read the same redacted value as the inspector; absent fields stay absent. */
export function eventPreview(event,reveal=false){
  const data=redact(event.data,reveal),value=data&&typeof data==='object'?data:{};
  const error=event.event==='console.error'||value.status==='FAILED'||Boolean(value.error);
  const category=error?'error':event.event==='answer.delta'?'answer':event.event==='cards'?'cards':event.event==='turn.finished'?'status':'other';
  let title=event.event,body='';
  if(event.event==='progress'){title='처리 중';body=reveal?String(value.message??'진행 상태 수신'):'진행 이벤트 수신 · 원문 기본 숨김';}
  else if(event.event==='answer.delta'){title='답변 조각';body=typeof event.data?.delta==='string'&&!event.data.delta.trim()?'빈 답변 조각 수신 · 추가 텍스트 없음':reveal?String(value.delta??'답변 데이터 없음'):`답변 ${typeof event.data?.delta==='string'?Array.from(event.data.delta).length:'미수집'}자 수신 · 원문 기본 숨김 · 위의 ‘답변·추천 이유 보기’를 켜세요`;}
  else if(event.event==='cards'){
    title='상품 추천';body=Array.isArray(value.products)?value.products.map(p=>`상품 #${p.productId??'미수집'} · ${p.reason??'추천 이유 없음'}`).join('\n'):'상품 데이터 없음';
  }else if(event.event==='turn.finished'){
    title=({COMPLETED:'답변 완료',FAILED:'생성 실패',CANCELLED:'작업 취소'})[value.status]??'최종 상태';
    const blocks=Array.isArray(value.blocks)?value.blocks:[];
    const cardCount=blocks.filter(b=>b.type==='cards').reduce((n,b)=>n+(Array.isArray(b.products)?b.products.length:0),0),cardOnly=value.status==='COMPLETED'&&cardCount>0&&!event.data.blocks.some(b=>b.type==='text'&&typeof b.text==='string'&&b.text.trim());
    body=[value.status,cardOnly?`상품 카드 ${cardCount}개로 응답했습니다.`:null,`${Array.isArray(value.blocks)?blocks.length+'개 블록':'블록 미수집'} · 저장 커서 ${value.lastEventId??'미수집'}`, value.error?.code,value.error?.message,reveal?blocks.filter(b=>b.type==='text').map(b=>b.text).join('\n'):null].filter(Boolean).join('\n');
  }else if(event.event==='console.accepted'){title='요청 접수';body=`HTTP ${value.httpStatus??'미수집'} · 서버 작업은 별도 확인`;}
  else if(event.event==='console.cancellation'){title='취소 응답';body=`HTTP ${value.httpStatus??'미수집'} · ${value.status??'상태 미수집'}`;}
  else if(error){title='연결 / 요청 오류';body=[value.httpStatus?'HTTP '+value.httpStatus:null,value.code??value.error?.code??'오류 데이터 수신'].filter(Boolean).join(' · ');}
  else body=reveal?(JSON.stringify(data,null,2)??'데이터 없음'):`수신 필드: ${Object.keys(value).join(', ')||'없음'} · 본문 숨김`;
  return {title,body,category,error};
}

export function requestErrorMessage(status,data={}){
  if(status===409&&['CONSOLE_CONVERSATION_NOT_OWNED','CONSOLE_CONVERSATION_UNCONFIRMED'].includes(data?.detail))return '대화 생성을 확인하지 못했습니다. 이 메시지는 아직 전송되지 않았습니다.';
  const labels={message:'채팅 메시지',recipientUserId:'수신자 ID',sourceVersion:'버전',giftPreference:'취향(최대500자)',dislikedCategories:'비선호 카테고리(최대5개)',categoryId:'카테고리 ID(양의 정수)',categoryName:'카테고리 이름',reviews:'리뷰(최대10개)',productId:'상품 ID(양의 정수)',rating:'별점(1~5 정수)',reviewText:'리뷰 본문'};
  const fields=Array.isArray(data?.detail)?[...new Set(data.detail.flatMap(e=>Array.isArray(e?.loc)?e.loc.filter(k=>typeof k==='string'&&Object.hasOwn(labels,k)).map(k=>labels[k]):[]))]:[];
  if(fields.length)return `HTTP ${status} · 입력 확인: ${fields.join(' / ')}`;
  return `HTTP ${status} · ${({400:'입력 형식 확인 필요',401:'인증 확인 필요',403:'접근 경계 확인 필요',404:'저장된 요청을 찾지 못함',409:'같은 버전·ID의 본문 충돌 또는 스냅샷 확인 필요',422:'필수 입력·길이·타입 확인 필요',429:'요청 한도 도달',503:'서비스·원장 준비 상태 확인'})[status]??'요청 처리 실패'}`;
}

export function parseProfileJSON(text,label){try{const value=JSON.parse(text);if(!Array.isArray(value))throw new Error();return value;}catch{throw new Error(label+'는 JSON 배열로 입력하세요.');}}
