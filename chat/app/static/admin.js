const $ = s => document.querySelector(s), el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
let next = null, current = null, detailPage = null, detailStart = 0, selectedFocus = null, allTurns = [], selectionVersion = 0, listVersion = 0, sessionVersion = 0;
const labels = {COMPLETED: '완료', FAILED: '실패', CANCELLED: '취소', PROCESSING: '생성 중'};
const when = s => s && Number.isFinite(Date.parse(s)) ? new Date(s).toLocaleString('ko-KR') : '미기록';
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
  const session = sessionVersion;
  return ChatHTTP.json(path, options, () => { if (session === sessionVersion) resetSession('관리자 세션이 만료됐습니다. 관리자 비밀번호로 다시 로그인하세요.'); });
}
function resetSession(message='관리자 로그인이 필요합니다.') {
  sessionVersion++; listVersion++; clearSelection(); next = null;
  $('#modal-key').value='';$('#modal-state').textContent='';
  connectionVersion++; connectionSnapshot = null; connectionBusy = false; connectionUncertain=false; $('#connection-active').textContent='';
  if (cf()?.elements) { cf().reset(); connectionState('로그인이 필요합니다.'); connectionButtons(); }
  $('#items').replaceChildren(); $('#login').hidden = false; $('#workspace').hidden = true; $('#logout').hidden = true; $('#status').textContent=message;
}
function report(e) { $('#status').textContent = e.message; $('#status').className = 'error'; }
function download(value, name) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'})), a = el('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function filters() {
  const data = new FormData($('#filters')), p = new URLSearchParams();
  for (const k of ['q', 'status', 'feedback', 'sort', 'issue']) if (data.get(k)) p.set(k, data.get(k));
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
      button.append(el('span', '대화 ' + row.id.slice(0, 8), 'title'), el('p', `${when(row.created)} · ${row.turnCount}턴`, 'meta'));
      if (row.latestTrace) {const t=row.latestTrace;button.append(el('p',`${t.status||'미기록'} · ${t.backend||'연결 미기록'} · ${t.model||'모델 미기록'} · 설정 v${t.connectionRevision??'?'} · ${t.sourceScope}`, 'meta'),el('p',`입/출 ${t.inputTokens??'?'}/${t.outputTokens??'?'} · ${t.elapsedMs??'?'}ms · ${t.costUsd??'?'} USD`,'meta'));}
      if (row.latestTrace) {const t=row.latestTrace;button.append(el('p',`해당 요청 ${when(t.created)} · ${diagnosticLabel(t.diagnostic?.category)}`,'meta'));}
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
  if (detailStart) { const first = el('button', '처음부터 보기'); first.onclick = () => select(current).catch(report); tools.append(first); head.append(el('p', '선택한 요청과 바로 앞 대화부터 표시합니다.', 'meta')); }
  head.append(tools); box.append(head); if (!allTurns.length) box.append(el('p', '아직 질문이 없는 대화입니다.', 'empty'));
  for (const turn of allTurns) {
    const article = el('article', undefined, 'turn' + (turn.turnId === selectedFocus ? ' focused-turn' : '')), top = el('div', undefined, 'turn-head');
    article.id = 'turn-' + turn.turnId; article.tabIndex = -1;
    top.append(el('span', when(turn.created)), el('span', labels[turn.state.status], 'badge' + (turn.state.status === 'FAILED' ? ' failed' : '')));
    article.append(top);
    if (!turn.trace) article.append(el('div', turn.state.message, 'question'));
    for (const block of turn.state.blocks) {
      if (block.type === 'text') article.append(el('div', block.text, 'answer'));
      if (block.type === 'cards') { const cards = el('div', undefined, 'cards'); for (const p of block.products) cards.append(el('p', `상품 ${p.productId} · ${p.reason}`)); article.append(cards); }
    }
    if (turn.state.error) article.append(el('p', turn.state.error.message, 'error'));
    if (!turn.state.blocks.length && !turn.trace) article.append(el('p', '저장된 답변이 없습니다.', 'meta'));
    const context = turn.context || {};
    if (!turn.trace) article.append(el('p', `${context.model || '모델 기록 없음'} · ${context.elapsedMs !== undefined ? (context.elapsedMs / 1000).toFixed(2) + '초' : '시간 기록 없음'}`, 'meta'));
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
$('#login').onsubmit = async e => { e.preventDefault(); try { await api('/admin/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: $('#password').value})}); resetSession(); await load(); await loadConnection(); } catch (e) { report(e); } finally { $('#password').value = ''; } };
$('#logout').onclick = async () => { try { await api('/admin/logout', {method: 'POST'}); resetSession(); $('#status').textContent = '로그아웃했습니다.'; } catch (e) { report(e); } };
load().then(()=>{if(!$('#workspace').hidden)return loadConnection();}).catch(report);

function diagnosticLabel(category) {
  return ({recipient_profile_unavailable:'수신자 취향 조회 실패',recipient_profile_stale:'수신자 취향 분석 버전 확인 필요',application_error:'AI 내부 처리 오류',turn_deadline:'AI 전체 처리 제한 시간 초과',provider_read_timeout:'모델 응답 읽기 시간 초과',provider_connect_timeout:'모델 연결 시간 초과',provider_write_timeout:'모델 요청 전송 시간 초과',provider_pool_timeout:'모델 연결 풀 대기 시간 초과',dependency_timeout:'조회·외부 의존 작업 시간 초과',dependency_http_error:'조회·외부 의존 서버 HTTP 오류',provider_http_error:'모델 서버 HTTP 오류',interrupted:'처리 중단 · 중단 원인은 별도 확인',failed:'AI 처리 실패',in_progress:'처리 중 · 마지막 저장 기록',completed:'AI 완료 기록 있음'})[category]||'원인 정보 미기록';
}
function diagnosticPanel(trace) {
  const d=trace.diagnostic||{},box=el('div',undefined,'trace-diagnostic');
  box.append(el('strong',diagnosticLabel(d.category)),el('p',`단계 ${d.stage||'미기록'} · 오류 ${d.code||'미기록'}${d.exceptionType&&d.exceptionType!==d.code?' · 예외 '+d.exceptionType:''} · ${d.httpStatusSource==='application'?'내부 상태':'HTTP 상태'} ${d.httpStatus??'미기록'}`,'meta'));
  if(trace.status==='FAILED'&&(!d.stage||!d.code||d.httpStatus==null))box.append(el('p','일부 실패 정보가 저장되지 않았습니다. 누락된 원인은 이 기록만으로 복원할 수 없습니다.','meta'));
  box.append(el('p',`AI 시작 ${when(d.startedAt)} · 마지막 기록 ${when(d.capturedAt)} · 전체 처리 제한 ${d.turnTimeoutSeconds??'미기록'}초`,'meta'));
  box.append(el('p',`첫 답변 조각 생성 ${d.firstAnswerMs??'미기록'}ms · 첫 답변 저장 ${d.firstCommittedDeltaMs??'미기록'}ms`,'meta'));
  if(d.checkpointWriteFailures)box.append(el('p',`중간 기록 저장 실패 ${d.checkpointWriteFailures}회 · 일부 진행 정보가 없을 수 있습니다.`,'meta'));
  box.append(el('p','AI 저장 기록입니다. BE·사용자 수신 여부와 AI에 도달하지 않은 요청은 이 기록만으로 확인할 수 없습니다.','meta'));
  return box;
}
function tracePanel(cid, turn) {
  const trace=turn.trace, box=el('section',undefined,'trace-panel');
  if(!trace){box.append(el('p','실행 근거 미수집 · 과거 기록은 재구성하지 않습니다.','meta'));return box;}
  const meta=el('div',undefined,'trace-meta'), totals=trace.totals||{};
  for(const value of [trace.sourceScope,trace.environment||'환경 미기록','코드 지문 '+(trace.sourceIdentity?.promptVersion||trace.sourceIdentity?.integrationSourceSha256||'미기록').slice(0,12),'빌드 커밋 '+(trace.sourceIdentity?.buildCommit||'미기록').slice(0,12),trace.connection?.model||turn.context?.model||'모델 미기록','설정 v'+(trace.connection?.revision??'미기록')])meta.append(el('span',value,'chip'));
  if(trace.executionMode==='mock')meta.prepend(el('span','합성 기록 · 모델 미호출','chip'));
  box.append(meta,el('p',`입력 ${totals.inputTokens??'미확인'} · 출력 ${totals.outputTokens??'미확인'} tokens · ${trace.elapsedMs??'미확인'}ms · 비용 ${totals.costUsd==null?'미확인':totals.costUsd+' USD'}`,'trace-totals'));
  box.append(el('p',`${trace.recording.calls==='not-recorded-legacy'?'모델 입출력 미기록':'마스킹된 모델 기록'} · 캐시/추론은 총 토큰에 포함됩니다.`,'meta'));
  box.append(diagnosticPanel(trace));
  const timeline=el('ol',undefined,'trace-timeline');
  for(const item of trace.timeline){const row=el('li');row.append(el('strong',item.kind==='model'?'모델 호출 '+item.index:item.name||'저장된 상태'),el('span',`${item.status||'미기록'} · ${item.elapsedMs??'미기록'} ms${item.errorType?' · '+item.errorType:''}`,'meta'));if(item.kind==='model'){row.append(el('p',`HTTP ${item.httpStatus??'미기록'} · 헤더 ${item.headersMs??'미기록'}ms · 첫 패킷 ${item.firstPacketMs??'미기록'}ms · 첫 텍스트 ${item.firstTextMs??'미기록'}ms · 첫 도구 ${item.firstToolMs??'미기록'}ms`,'meta'),el('p',`모델 연결 제한 ${item.timeouts?.connectSeconds??'미기록'}초 · 읽기 제한 ${item.timeouts?.readSeconds??'미기록'}초 (응답 전체가 아닌 읽기 대기 제한)`,'meta'));}if(item.productIds)row.append(el('p','상품 후보 '+item.productIds.length+'개 · '+item.productIds.join(', '),'meta'));timeline.append(row);}
  box.append(timeline);
  const technical=el('details'),technicalTitle=el('summary','식별자·SSE 기록');technical.append(technicalTitle,el('p',`대화 ${cid} · 요청 ${turn.turnId} · ${trace.environment||'환경 미기록'} · 모델 ${trace.connection?.baseUrl||'주소 미기록'}`,'meta'));
  technical.append(el('p',`빌드 커밋 ${trace.sourceIdentity?.buildCommit||'미기록'} · 코드 지문은 Git 커밋이 아닙니다. 과거 빌드는 당시 이미지 태그·digest로 확인하세요.`,'meta'));
  const sse=trace.sseEvents;technical.append(el('p',sse?.events?.length?(sse.basis?.startsWith('stored SSE')?'저장된 SSE: ':'합성/외부 기록 이벤트: ')+sse.events.map(e=>e.id+' '+e.event).join(' → ')+(sse.truncated?' · 앞 100개만 표시':''):'SSE 이벤트 목록 미수집 · 최종 상태로 브라우저 수신을 추정하지 않습니다.','meta'));box.append(technical);
  const exportTrace=el('button','진단 기록 저장');exportTrace.onclick=()=>download(trace,'ai-trace-'+turn.turnId+'.json');box.append(exportTrace);
  const action=el('button','입출력 상세 열람'), detail=el('div'), state=el('p','','meta'); let open=false, busy=false;
  action.onclick=async()=>{
    if(busy)return;if(open){detail.replaceChildren();open=false;action.textContent='입출력 상세 열람';return;}
    const generation=selectionVersion;busy=true;action.disabled=true;state.textContent='마스킹된 상세를 불러오는 중…';
    try{const data=await api(`/admin/api/conversations/${cid}/turns/${turn.turnId}/trace?details=true`);if(generation!==selectionVersion||!box.isConnected)return;detail.append(traceDetail(data));state.textContent=data.recording.detailState==='bounded-redacted'?(data.executionMode==='mock'?'합성 저장 예시 · 실제 모델 미호출':'실제 저장 기록 · 비공개 취향/비밀/추론 원문 제외'):'원문 보존 기간 만료 또는 과거 미기록';open=true;action.textContent='상세 닫기';}
    catch(e){state.textContent=e.message;}finally{busy=false;action.disabled=false;}
  };box.append(action,state,detail);return box;
}
function traceDetail(data){
  const root=el('div',undefined,'io-detail');
  if(data.publicInput!==undefined){root.append(el('h3','사용자 입력'),el('p',data.publicInput,'io-message'),el('h3','최종 응답'));
    for(const block of data.publicOutput||[]){if(block.text)root.append(el('p',block.text,'io-message'));for(const card of block.products||[]){const row=el('div',undefined,'io-card');row.append(el('strong','상품 '+card.productId),el('p',card.reason));root.append(row);}}
  }else root.append(el('p','원문 열람 기간이 지났거나 생성 시각이 기록되지 않았습니다.','meta'));
  if(data.noteRecords?.truncated)root.append(el('p',`메모·의견 일부만 표시됩니다. 검토 메모 ${data.noteRecords.reviewsTotal}개, 의견 ${data.noteRecords.feedbackTotal}개 중 종류별 최근 ${data.noteRecords.limitPerKind}개까지 표시합니다.`,'meta'));
  for(const [title,notes] of [['검토 메모',data.reviewNotes],['사용자 의견',data.feedbackNotes]]){if(notes?.length){root.append(el('h3',title));for(const note of notes)root.append(el('p',[note.verdict,note.note].filter(Boolean).join(' · '),'io-message'));}}
  for(const call of data.modelCalls||[]){const detail=el('details'),sum=el('summary',`모델 호출 ${call.index} · ${call.status||'미기록'} · ${call.usage?.inputTokens??'?'} → ${call.usage?.outputTokens??'?'} tokens`);detail.append(sum,el('h4',call.input?.capture==='actual-send-time-redacted'?'실제 전송 시점의 마스킹 입력':'저장된 입력 · 실제 전송 여부 미확인'),el('pre',call.input?JSON.stringify(call.input,null,2):'미기록 또는 열람 기간 만료'),el('h4','모델 반환 기록'),el('pre',call.output?JSON.stringify(call.output,null,2):'미기록 또는 열람 기간 만료'));root.append(detail);}
  if(!data.modelCalls?.length)root.append(el('p','과거 모델 입출력은 기록되지 않았습니다. 사용량 영수증만 별도로 확인할 수 있습니다.','meta'));
  root.append(el('p','비공개 취향·비밀·추론 원문과 도구 인자 본문은 이 상세에 포함하지 않습니다.','meta'));return root;
}
let connectionSnapshot=null, connectionBusy=false, connectionVersion=0, connectionUncertain=false;
const cf=()=>$('#connection-form');
function connectionInput(){
 const value=name=>cf().elements[name].value;
 return {kind:'openai-compatible',baseUrl:value('baseUrl').trim(),model:value('model').trim(),connectTimeout:Number(value('connectTimeout')),readTimeout:Number(value('readTimeout'))};
}
function connectionState(message){$('#connection-status').textContent=message;}
function connectionButtons(){
 if(!cf()?.elements)return;
 for(const input of cf().elements)input.disabled=connectionBusy||connectionUncertain||!connectionSnapshot;
 $('#connection-reload').disabled=connectionBusy;
 $('#connection-openrouter').disabled=connectionBusy||connectionUncertain||!connectionSnapshot;
 $('#connection-apply').disabled=connectionBusy||connectionUncertain||!connectionSnapshot?.openrouterReady;
}
function fillConnection(){
 cf().reset();$('#modal-key').value='';
 if(connectionSnapshot?.endpointActive){for(const [k,v] of Object.entries(connectionSnapshot.config))if(cf().elements[k])cf().elements[k].value=v;}
}
function showConnection(data){
 if(!data||!Number.isSafeInteger(data.revision)||!data.config||typeof data.config!=='object')throw new Error('연결 상태를 읽지 못했습니다. 다시 조회하세요.');
 connectionSnapshot=data;connectionUncertain=false;fillConnection();
 $('#connection-active').textContent=`현재 적용: ${data.endpointActive?'직접 연결':'OpenRouter'} / ${data.config.model} / ${data.config.baseUrl}`;
 $('#modal-state').textContent=data.openrouterReady?'서버 메모리 설정, 재시작 후 OpenRouter 기본 연결':'차단: 기본 OpenRouter 역할별 키를 준비하세요.';
 connectionState('현재 연결을 확인했습니다. 키는 조회하거나 화면에 다시 표시하지 않습니다.');connectionButtons();
}
async function loadConnection(){
 if(connectionBusy)return;
 const generation=++connectionVersion;connectionBusy=true;connectionButtons();
 try{const data=await api('/admin/api/model-connection');if(generation===connectionVersion)showConnection(data);}
 catch(e){if(generation===connectionVersion)connectionState(e.message);}
 finally{if(generation===connectionVersion){connectionBusy=false;connectionButtons();}}
}
async function connectionAction(path,body){
 if(connectionBusy||connectionUncertain)return;
 const generation=++connectionVersion;connectionBusy=true;connectionButtons();connectionState('연결을 변경하고 있습니다…');
 try{
  const data=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(generation===connectionVersion)showConnection(data);
 }catch(e){
  if(generation!==connectionVersion)return;
  if(!e.status||e.status>=500){connectionUncertain=true;$('#connection-active').textContent=$('#connection-active').textContent.replace('현재 적용:','마지막 확인:');$('#modal-state').textContent='현재 적용 여부 미확정';connectionState('응답을 확인하지 못했습니다. 자동 재전송하지 않습니다. 현재 연결을 다시 조회하세요.');}
  else connectionState(e.message);
 }finally{delete body.key;if(generation===connectionVersion){connectionBusy=false;connectionButtons();}}
}
if(cf()?.elements&&$('#tab-traces')){
 $('#tab-traces').onclick=()=>{$('#traces-view').hidden=false;$('#connection-view').hidden=true;$('#tab-traces').setAttribute('aria-pressed','true');$('#tab-connection').setAttribute('aria-pressed','false');};
 $('#tab-connection').onclick=()=>{$('#traces-view').hidden=true;$('#connection-view').hidden=false;$('#tab-traces').setAttribute('aria-pressed','false');$('#tab-connection').setAttribute('aria-pressed','true');loadConnection();};
 $('#connection-reload').onclick=loadConnection;
 $('#connection-openrouter').onclick=()=>{if(connectionSnapshot)connectionAction('/admin/api/model-connection/modal-off',{revision:connectionSnapshot.revision});};
 $('#connection-cancel').onclick=()=>{if(!connectionBusy&&!connectionUncertain)fillConnection();};
 cf().onsubmit=async e=>{
  e.preventDefault();if(connectionBusy||connectionUncertain||!connectionSnapshot)return;
  const key=$('#modal-key').value;$('#modal-key').value='';
  await connectionAction('/admin/api/model-connection/activate',{revision:connectionSnapshot.revision,config:connectionInput(),key});
 };
 for(const [id,status,issue] of [['requests-attention','','attention'],['requests-slow','','slow'],['requests-processing','PROCESSING',''],['requests-failed','FAILED',''],['requests-cancelled','CANCELLED',''],['requests-all','','']])$('#'+id).onclick=()=>{const f=$('#filters').elements;f.status.value=status;if(f.issue)f.issue.value=issue;if(f.sort)f.sort.value='activity';load().catch(report);};
}
