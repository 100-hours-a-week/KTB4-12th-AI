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
      button.append(el('span', row.title, 'title'), el('p', `${when(row.created)} · ${row.turnCount}턴 · 의견 ${row.feedbackCount}`, 'meta'));
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
    if (!turn.state.blocks.length) article.append(el('p', '저장된 답변이 없습니다.', 'meta'));
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
    if (Array.isArray(turn.reviews)) article.append(reviewEditor(turn, data.conversationId));
    const details = el('details'), summary = el('summary', '검색 · 수신자 정보 · 모델 설정 · 오류 기록');
    details.append(summary, el('pre', JSON.stringify({turnId: turn.turnId, eventCount: turn.state.lastEventId, ...context}, null, 2))); article.append(details); box.append(article);
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
