const $ = s => document.querySelector(s), el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
let next = null, current = null, detailPage = null, detailStart = 0, selectedFocus = null, allTurns = [], selectionVersion = 0, listVersion = 0;
const labels = {COMPLETED: '완료', FAILED: '실패', CANCELLED: '취소', PROCESSING: '생성 중'};
const when = s => new Date(s).toLocaleString('ko-KR');
function traceMessage(context) {
  const marker = context?.tracePersistence;
  if (marker === undefined || marker === null) return '추적 저장 상태 미기록(기존 자료)';
  const messages = {recorded: '실행 추적 저장됨', interrupted: '중단 시점의 실행 추적 저장됨', not_recorded: '실행 추적 저장 누락', pending: '실행 추적 저장 대기'};
  return marker.version === 1 && Object.hasOwn(messages, marker.status) ? messages[marker.status] : '추적 저장 상태 확인 필요';
}
function reviewNote(review) {
  const note = el('div', undefined, 'review-note');
  note.append(el('p', when(review.created), 'meta'), el('p', review.note));
  return note;
}
function reviewEditor(turn, cid) {
  const section = el('section', undefined, 'evaluation'), notes = el('div'), form = el('form', undefined, 'review-form');
  section.append(el('h3', '관리자 평가'), notes);
  for (const review of turn.reviews) notes.append(reviewNote(review));
  const label = el('label', '평가 메모'), input = el('textarea'), save = el('button', '메모 저장'), status = el('p', '', 'meta');
  input.rows = 3; input.maxLength = 1500; input.required = true;
  input.placeholder = '판단과 근거를 적어 주세요. 근거가 부족하면 판단 보류로 남겨 주세요.';
  save.type = 'submit'; status.setAttribute('role', 'status'); label.append(input); form.append(label, save, status); section.append(form);
  let pending = null, saving = false;
  form.onsubmit = async event => {
    event.preventDefault(); const note = input.value.trim(); if (!note || saving) return;
    if (!pending || pending.note !== note) pending = {reviewId: crypto.randomUUID(), note};
    saving = true; save.disabled = input.disabled = true; status.textContent = '저장하고 있습니다…'; status.className = 'meta';
    try {
      const result = await api(`/admin/api/conversations/${cid}/turns/${turn.turnId}/reviews/${pending.reviewId}`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({note: pending.note})});
      if (!turn.reviews.some(review => review.reviewId === result.reviewId)) {
        const review = {reviewId: result.reviewId, note: result.note, created: result.created};
        turn.reviews.push(review); notes.append(reviewNote(review));
      }
      input.value = ''; pending = null; status.textContent = '평가 메모를 저장했습니다.';
    } catch (error) { status.textContent = error.message; status.className = 'error'; }
    finally { saving = false; save.disabled = input.disabled = false; }
  };
  return section;
}
function clearSelection() { selectionVersion++; current = null; selectedFocus = null; allTurns = []; $('#detail').replaceChildren(); }
async function api(path, options = {}) {
  return ChatHTTP.json(path, options, () => { listVersion++; clearSelection(); $('#items').replaceChildren(); $('#login').hidden = false; $('#workspace').hidden = true; $('#logout').hidden = true; });
}
function report(e) { $('#status').textContent = e.message; $('#status').className = 'error'; }
function download(value, name) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'})), a = el('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function filters() {
  const data = new FormData($('#filters')), p = new URLSearchParams();
  for (const k of ['q', 'status', 'feedback', 'sort']) if (data.get(k)) p.set(k, data.get(k));
  for (const k of ['since', 'until']) if (data.get(k)) { const d = new Date(data.get(k) + 'T00:00:00'); if (k === 'until') d.setDate(d.getDate() + 1); p.set(k, d.toISOString()); }
  return p;
}
async function load(reset = true) {
  const generation = ++listVersion, p = filters(); if (!reset && next) p.set('cursor', next);
  $('#more').disabled = true; $('#status').className = ''; $('#status').textContent = '대화를 불러오고 있습니다…';
  try {
    const data = await api('/admin/api/conversations?' + p); if (generation !== listVersion) return;
    $('#login').hidden = true; $('#workspace').hidden = false; $('#logout').hidden = false; $('#status').className = '';
    if (reset) {
      $('#items').replaceChildren();
      if (current && !data.items.some(r => r.id === current)) { clearSelection(); $('#detail').replaceChildren(el('p', '대화를 선택해 주세요.', 'empty')); }
    }
    next = data.nextCursor;
    for (const row of data.items) {
      const button = el('button', undefined, 'conversation'); button.dataset.id = row.id; button.setAttribute('aria-pressed', row.id === current);
      button.append(el('span', '대화 ' + row.id.slice(0, 8), 'title'), el('p', `${when(row.created)} · ${row.turnCount}턴 · 의견 ${row.feedbackCount}`, 'meta'));
      if (row.latestTrace) {const t=row.latestTrace;button.append(el('p',`${t.status||'미기록'} · ${t.model||'모델 미기록'} · ${t.sourceScope} · ${t.environment||'환경 미기록'}`, 'meta'),el('p',`요청 ${t.turnId?.slice(0,8)||'—'} · v${t.sourceVersion??'—'} · ${t.revision?.slice(0,8)||'버전 미기록'} · 입/출 ${t.inputTokens??'?'}/${t.outputTokens??'?'} · ${t.elapsedMs??'?'}ms · ${t.costUsd??'?'} USD`,'meta'));}
      if (row.latestFeedbackAt) button.append(el('p', `최근 의견 ${when(row.latestFeedbackAt)} · 미검토 ${row.pendingFeedbackCount}개`, 'meta'));
      if (row.failedCount) button.append(el('span', `실패 ${row.failedCount}건`, 'meta failed'));
      button.onclick = () => select(row.id, false, row.focusTurnId).catch(report); $('#items').append(button);
    }
    $('#more').hidden = !next; $('#status').textContent = $('#items').children.length + '개 대화 표시';
    if (!$('#items').children.length) $('#items').append(el('p', '조건에 맞는 대화가 없습니다. 필터를 바꿔 보세요.', 'empty'));
  } finally { $('#more').disabled = false; }
}
async function select(cid, append = false, focus = null) {
  const generation = ++selectionVersion, params = new URLSearchParams();
  if (!append) { current = cid; detailPage = null; detailStart = 0; selectedFocus = focus; allTurns = []; $('#detail').replaceChildren(el('p', '대화를 불러오고 있습니다…', 'empty')); }
  if (append) params.set('after', detailPage); else if (focus) params.set('focusTurnId', focus);
  const data = await api('/admin/api/conversations/' + cid + '?' + params); if (generation !== selectionVersion) return;
  if (!append) detailStart = data.startCursor || 0;
  allTurns.push(...data.turns); detailPage = data.nextCursor;
  document.querySelectorAll('.conversation').forEach(b => b.setAttribute('aria-pressed', b.dataset.id === cid));
  render(data);
  if (!append && focus) { const target = document.getElementById('turn-' + focus); if (target) { target.scrollIntoView({behavior: 'auto', block: 'center'}); target.focus({preventScroll: true}); } }
}
function render(data) {
  const box = $('#detail'); box.replaceChildren(); const head = el('div', undefined, 'detail-head');
  head.append(el('h2', '대화 기록'), el('p', `발신자 ${data.senderUserId} · 수신자 ${data.recipientUserId}`, 'meta'), el('p', data.conversationId, 'meta'));
  const tools = el('div', undefined, 'tools'), exportButton = el('button', '표시된 턴 JSON 저장'), refresh = el('button', '대화 새로고침');
  exportButton.onclick = () => download({...data, turns: allTurns, startCursor: detailStart, nextCursor: detailPage}, 'conversation-' + current + '.json');
  refresh.onclick = () => select(current, false, selectedFocus).catch(report); tools.append(exportButton, refresh);
  if (detailStart) { const first = el('button', '처음부터 보기'); first.onclick = () => select(current).catch(report); tools.append(first); head.append(el('p', '의견이 달린 질문과 바로 앞 대화부터 표시합니다.', 'meta')); }
  head.append(tools); box.append(head); if (!allTurns.length) box.append(el('p', '아직 질문이 없는 대화입니다.', 'empty'));
  for (const turn of allTurns) {
    const article = el('article', undefined, 'turn' + (turn.turnId === selectedFocus ? ' focused-turn' : '')), top = el('div', undefined, 'turn-head');
    article.id = 'turn-' + turn.turnId; article.tabIndex = -1;
    top.append(el('span', when(turn.created)), el('span', labels[turn.state.status], 'badge' + (turn.state.status === 'FAILED' ? ' failed' : '')));
    article.append(top, el('div', turn.state.message, 'question'), el('span', '선잘알', 'role'));
    for (const block of turn.state.blocks) {
      if (block.type === 'text') article.append(el('div', block.text, 'answer'));
      if (block.type === 'cards') { const cards = el('div', undefined, 'cards'); for (const p of block.products) cards.append(el('p', `상품 ${p.productId} · ${p.reason}`)); article.append(cards); }
    }
    if (turn.state.error) article.append(el('p', turn.state.error.message, 'error'));
    if (!turn.state.blocks.length) article.append(el('p', turn.trace?'답변 원문은 입출력 상세 열람에서 확인합니다.':'저장된 답변이 없습니다.', 'meta'));
    const context = turn.context || {}, profile = context.recipientProfile;
    if (profile?.source === 'TEST_FIXTURE') article.append(el('p', '테스트 수신자: ' + profile.label + ' · 선호: ' + profile.data.preferredTags.join(', ') + ' · 비선호: ' + profile.data.dislikedTags.join(', '), 'meta'));
    article.append(el('p', `${context.model || '모델 기록 없음'} · ${context.elapsedMs !== undefined ? (context.elapsedMs / 1000).toFixed(2) + '초' : '시간 기록 없음'} · 의견 ${turn.feedback.length}개`, 'meta'));
    article.append(el('p', traceMessage(context), 'meta' + (context.tracePersistence?.version === 1 && context.tracePersistence.status === 'not_recorded' ? ' failed' : '')));
    for (const feedback of turn.feedback) {
      const note = el('div', `${feedback.payload.reporterName || '익명'} · ${feedback.payload.verdict}\n${feedback.payload.note || ''}`, 'feedback'), button = el('button', feedback.reviewed ? '검토 완료 ✓' : '검토 완료로 표시');
      button.onclick = async () => {
        button.disabled = true;
        try { await api('/admin/api/feedback/' + feedback.id, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({reviewed: !feedback.reviewed})}); feedback.reviewed = !feedback.reviewed; button.textContent = feedback.reviewed ? '검토 완료 ✓' : '검토 완료로 표시'; await load(); }
        catch (e) { report(e); } finally { button.disabled = false; }
      };
      note.append(button); article.append(note);
    }
    if (Array.isArray(turn.reviews)) {const review=el('details',undefined,'review-fold');review.append(el('summary','검토 메모'),reviewEditor(turn,data.conversationId));article.append(review);}
    article.append(tracePanel(data.conversationId, turn)); box.append(article);
  }
  if (detailPage) {
    const more = el('button', '다음 턴 보기'); more.onclick = async () => { more.disabled = true; try { await select(current, true); } catch (e) { report(e); more.disabled = false; } }; box.append(more);
  }
}
$('#filters').onsubmit = e => { e.preventDefault(); load().catch(report); };
$('#more').onclick = () => load(false).catch(report);
$('#login').onsubmit = async e => { e.preventDefault(); try { await api('/admin/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: $('#password').value})}); await load(); } catch (e) { report(e); } finally { $('#password').value = ''; } };
$('#logout').onclick = async () => { try { await api('/admin/logout', {method: 'POST'}); listVersion++; clearSelection(); $('#items').replaceChildren(); $('#workspace').hidden = true; $('#logout').hidden = true; $('#login').hidden = false; $('#status').textContent = '로그아웃했습니다.'; } catch (e) { report(e); } };
load().catch(report);

function tracePanel(cid, turn) {
  const trace=turn.trace, box=el('section',undefined,'trace-panel');
  if(!trace){box.append(el('p','실행 근거 미수집 · 과거 기록은 재구성하지 않습니다.','meta'));return box;}
  const meta=el('div',undefined,'trace-meta'), totals=trace.totals||{};
  for(const value of [trace.sourceScope,trace.environment||'환경 미기록',trace.revision?.slice(0,12)||'버전 미기록',trace.connection?.model||turn.context?.model||'모델 미기록'])meta.append(el('span',value,'chip'));
  if(trace.executionMode==='mock')meta.prepend(el('span','합성 UI fixture · 실제 모델 미호출','chip'));
  box.append(meta,el('p',`입력 ${totals.inputTokens??'미확인'} · 출력 ${totals.outputTokens??'미확인'} tokens · 비용 ${totals.costUsd===null?'미확인':totals.costUsd+' USD'}`,'trace-totals'));
  box.append(el('p',`요청 ${turn.turnId} · ${trace.recording.calls} · 캐시/추론은 총 토큰에 다시 더하지 않습니다.`,'meta'));
  const timeline=el('ol',undefined,'trace-timeline');
  for(const item of trace.timeline){const row=el('li');row.append(el('strong',item.kind==='model'?'모델 호출 '+item.index:item.name||'저장된 최종 상태'),el('span',`${item.status||'미기록'}${item.elapsedMs!==undefined?' · '+item.elapsedMs+' ms':''}`,'meta'));if(item.productIds)row.append(el('p','상품 후보 '+item.productIds.length+'개 · '+item.productIds.join(', '),'meta'));timeline.append(row);}
  box.append(timeline);
  const sse=trace.sseEvents;box.append(el('p',sse?.events?.length?(sse.basis?.startsWith('stored SSE')?'저장된 SSE: ':'합성/외부 기록 이벤트: ')+sse.events.map(e=>e.id+' '+e.event).join(' → ')+(sse.truncated?' · 앞 100개만 표시':''):'SSE 이벤트 목록 미수집 · 최종 상태로 브라우저 수신을 추정하지 않습니다.','meta'));
  const action=el('button','입출력 상세 열람'), detail=el('div'), state=el('p','','meta'); let open=false, busy=false;
  action.onclick=async()=>{
    if(busy)return;if(open){detail.replaceChildren();open=false;action.textContent='입출력 상세 열람';return;}
    const generation=selectionVersion;busy=true;action.disabled=true;state.textContent='마스킹된 상세를 불러오는 중…';
    try{const data=await api(`/admin/api/conversations/${cid}/turns/${turn.turnId}/trace?details=true`);if(generation!==selectionVersion||!box.isConnected)return;detail.append(traceDetail(data));state.textContent=data.recording.detailState==='bounded-redacted'?'실제 저장 기록 · 비공개 취향/비밀/추론 원문 제외':'원문 보존 기간 만료 또는 과거 미기록';open=true;action.textContent='상세 닫기';}
    catch(e){state.textContent=e.message;}finally{busy=false;action.disabled=false;}
  };box.append(action,state,detail);return box;
}
function traceDetail(data){
  const root=el('div',undefined,'io-detail');
  if(data.publicInput!==undefined){root.append(el('h3','사용자 입력'),el('p',data.publicInput,'io-message'),el('h3','최종 응답'));
    for(const block of data.publicOutput||[]){if(block.text)root.append(el('p',block.text,'io-message'));for(const card of block.products||[]){const row=el('div',undefined,'io-card');row.append(el('strong','상품 '+card.productId),el('p',card.reason));root.append(row);}}
  }else root.append(el('p','원문 열람 기간이 지났거나 생성 시각이 기록되지 않았습니다.','meta'));
  for(const call of data.modelCalls||[]){const detail=el('details'),sum=el('summary',`모델 호출 ${call.index} · ${call.status||'미기록'} · ${call.usage?.inputTokens??'?'} → ${call.usage?.outputTokens??'?'} tokens`);detail.append(sum,el('h4','실제 전송 시점의 마스킹 입력'),el('pre',call.input?JSON.stringify(call.input,null,2):'미기록 또는 열람 기간 만료'),el('h4','모델 반환 기록'),el('pre',call.output?JSON.stringify(call.output,null,2):'미기록 또는 열람 기간 만료'));root.append(detail);}
  if(!data.modelCalls?.length)root.append(el('p','과거 모델 입출력은 기록되지 않았습니다. 사용량 영수증만 별도로 확인할 수 있습니다.','meta'));
  root.append(el('p','비공개 취향·비밀·추론 원문과 도구 인자 본문은 이 상세에 포함하지 않습니다.','meta'));return root;
}
let connectionSnapshot=null, connectionBusy=false, connectionVersion=0;
const cf=()=>$('#connection-form');
function connectionInput(){const form=new FormData(cf());return {kind:form.get('kind'),baseUrl:form.get('baseUrl').trim(),model:form.get('model').trim(),auth:form.get('auth'),secretRef:form.get('secretRef')||null,connectTimeout:Number(form.get('connectTimeout')),readTimeout:Number(form.get('readTimeout'))};}
function connectionState(message){$('#connection-status').textContent=message;}
function connectionButtons(){for(const id of ['connection-save','connection-check','connection-rollback','connection-reload'])$('#'+id).disabled=connectionBusy||(id!=='connection-reload'&&!connectionSnapshot?.writable)||(id==='connection-check'&&(!$('#probe-consent').checked||cf().elements.kind.value!=='openai-compatible'))||(id==='connection-rollback'&&!connectionSnapshot?.history?.length);}
function showConnection(data){connectionSnapshot=data;for(const [k,v]of Object.entries(data.config)){if(k==='secretRef')continue;if(cf().elements[k])cf().elements[k].value=v??'';}const refs=cf().elements.secretRef;refs.replaceChildren(new Option('없음',''));for(const name of data.secretReferences||[])refs.append(new Option(name,name));refs.value=data.config.secretRef||'';const caps=$('#connection-capabilities');caps.replaceChildren();for(const [key,label]of [['connection','연결'],['tools','도구 호출'],['sse','SSE 완료'],['jsonSchema','별도 JSON Schema']])caps.append(el('div',label+' · '+(data.verification?.[key]===true?'명시 검증 확인':'미관측'),'capability'));connectionState(`설정 v${data.revision} · ${data.writable?'저장 가능':'읽기 전용'} · ${data.scope}`);connectionButtons();}
async function loadConnection(){const generation=++connectionVersion;connectionBusy=true;connectionButtons();try{const data=await api('/admin/api/model-connection');if(generation===connectionVersion)showConnection(data);}catch(e){connectionState(e.message);}finally{connectionBusy=false;connectionButtons();}}
async function connectionAction(path,body){if(connectionBusy)return;connectionBusy=true;connectionButtons();try{const data=await api(path,{method:path.endsWith('model-connection')?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(data.config)showConnection(data);else connectionState('도구·SSE 호환 확인. 이 설정은 아직 적용하지 않았습니다.');}catch(e){connectionState(e.message+' 기존 적용 설정은 다시 조회해 확인하세요.');}finally{connectionBusy=false;connectionButtons();}}
if (cf()?.elements && $('#tab-traces')) {
$('#tab-traces').onclick=()=>{$('#traces-view').hidden=false;$('#connection-view').hidden=true;$('#tab-traces').setAttribute('aria-pressed','true');$('#tab-connection').setAttribute('aria-pressed','false');};
$('#tab-connection').onclick=()=>{$('#traces-view').hidden=true;$('#connection-view').hidden=false;$('#tab-traces').setAttribute('aria-pressed','false');$('#tab-connection').setAttribute('aria-pressed','true');loadConnection();};
$('#connection-reload').onclick=loadConnection;
$('#probe-consent').onchange=connectionButtons;
cf().elements.kind.onchange=()=>{cf().elements.auth.value=cf().elements.kind.value==='openrouter'?'existing-openrouter':'none';if(cf().elements.kind.value==='openrouter')cf().elements.baseUrl.value='https://openrouter.ai/api/v1';connectionButtons();};
cf().onsubmit=e=>{e.preventDefault();if(connectionSnapshot)connectionAction('/admin/api/model-connection',{revision:connectionSnapshot.revision,config:connectionInput()});};
$('#connection-check').onclick=()=>{if($('#probe-consent').checked)connectionAction('/admin/api/model-connection/check',{config:connectionInput()});};
$('#connection-rollback').onclick=()=>{if(connectionSnapshot)connectionAction('/admin/api/model-connection/rollback',{revision:connectionSnapshot.revision,config:{}});};

}
