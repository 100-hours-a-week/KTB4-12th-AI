const $ = s => document.querySelector(s);
let cid = null, busy = false, active = null, streamController = null;
let feedbackTurn = null, feedbackSubmission = null, authenticated = false;
let viewVersion = 0, streamVersion = 0, historyVersion = 0;
let composing = false, followLatest = true;
let recipientOptions = [], selectedRecipient = null;
let mockMode = false, runtimeMock = false, protocolFixtureMode = false;
let modelApplying = false;
const turns = new Map(), PENDING_KEY = 'chatPendingTurn';
let pendingTurn = readPending();

function readPending() {
  try {
    const p = JSON.parse(sessionStorage.getItem(PENDING_KEY));
    if (p && typeof p.cid === 'string' && typeof p.tid === 'string' && typeof p.message === 'string') return p;
  } catch {}
  return null;
}
function rememberPending(p) {
  pendingTurn = p;
  try { if (p) sessionStorage.setItem(PENDING_KEY, JSON.stringify(p)); else sessionStorage.removeItem(PENDING_KEY); } catch {}
}
async function api(path, method = 'GET', body) {
  return ChatHTTP.json(path, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined}, lock);
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function answerText(node, text) {
  node.replaceChildren();
  for (const part of text.replace(/^\s*\*\s+/gm, '• ').split(/(\*\*[^*]+\*\*)/g)) {
    node.append(part.startsWith('**') && part.endsWith('**') ? el('strong', '', part.slice(2, -2)) : document.createTextNode(part));
  }
}
function notice(text = '', recover = false) { $('#notice').textContent = text; $('#reconnect').hidden = !recover; }
function scroll(force = false) {
  if (force) followLatest = true;
  if (followLatest) { const s = $('#scroll'); s.scrollTop = s.scrollHeight; }
  $('#latest').hidden = followLatest;
}
const touchKeyboard = () => window.matchMedia?.('(pointer: coarse)').matches || false;
const draftKey = () => 'chatDraft:' + (cid || 'new');
function saveDraft() {
  if (!authenticated) return;
  try { const text = $('#message').value; if (text) sessionStorage.setItem(draftKey(), text); else sessionStorage.removeItem(draftKey()); } catch {}
}
function updateComposer(save = true) {
  const input = $('#message');
  const count = Array.from(input.value.trim()).length;
  $('#count').textContent = count + ' / 200';
  $('#send').disabled = busy || modelApplying || Boolean(pendingTurn) || !authenticated || !count || count > 200;
  input.style.height = 'auto'; input.style.height = Math.min(160, Math.max(52, input.scrollHeight || 52)) + 'px';
  if (save) saveDraft();
}
function restoreDraft() {
  try { $('#message').value = sessionStorage.getItem(draftKey()) || ''; } catch { $('#message').value = ''; }
  updateComposer(false);
}
function editQuestion(text) {
  if (!authenticated || busy || pendingTurn) return;
  $('#message').value = text; updateComposer(); $('#message').focus();
}
async function loadRecipients() {
  const result = await api('/demo/recipients');
  if (!Array.isArray(result.recipients)) throw new Error('테스트 수신자를 불러오지 못했습니다. 다시 접속해 주세요.');
  recipientOptions = result.recipients;
  $('#recipient').replaceChildren(new Option('선택 안 함 · 대화로 지정', 'none'));
  for (const item of recipientOptions) $('#recipient').append(new Option(item.label, item.key));
}
function setRecipient(profile, draft = false) {
  selectedRecipient = profile || null;
  const key = profile?.key || 'none';
  if (profile && !recipientOptions.some(item => item.key === key)) $('#recipient').append(new Option(profile.label, key));
  $('#recipient').value = key;
  const data = profile?.data;
  $('#recipient-preferences').textContent = data ? '선호: ' + (data.preferredTags.join(', ') || '없음') + ' · 비선호: ' + (data.dislikedTags.join(', ') || '없음') : '대화에서 대상과 조건을 지정하세요.';
  $('#recipient-help').textContent = cid ? '이 대화의 수신자입니다. 변경하려면 새 대화를 시작하세요.' : '테스트 프로필이며 대화를 시작하면 수신자가 고정됩니다.';
  $('#recipient').disabled = !authenticated || busy || Boolean(cid);
  if (draft) try { sessionStorage.setItem('chatRecipient:new', key); } catch {}
}
function restoreRecipient() {
  let key = 'none'; try { key = sessionStorage.getItem('chatRecipient:new') || key; } catch {}
  setRecipient(recipientOptions.find(item => item.key === key));
}
function stopStream(cancelReveal = true) { if (cancelReveal) for (const ui of turns.values()) ui.reveal?.cancel(); streamVersion++; if (streamController) streamController.abort(); streamController = null; active = null; }
function setBusy(value) {
  busy = value;
  updateComposer(false);
  $('#stop').hidden = !value || !active;
  $('#new').disabled = value || !authenticated;
  $('#protocol-fixture').disabled = value || !authenticated || Boolean(cid) || runtimeMock;
  $('#my-reports').disabled = !authenticated;
  $('#report-session').disabled = value || !authenticated || Boolean(pendingTurn);
  $('#recipient').disabled = value || !authenticated || Boolean(cid);
  $('#history-toggle').disabled = value || !authenticated;
  $('#message').disabled = !authenticated;
  document.querySelectorAll('.examples button').forEach(button => { button.disabled = value || !authenticated; });
  $('#mock-scenario').disabled = value || !authenticated;
  $('#mock-resume').hidden = !mockMode || !active;
  $('#status').textContent = !authenticated ? '체험 코드 입력' : value ? (active ? '생성 중' : pendingTurn ? '접수 확인 중' : '불러오는 중') : '대기';
  $('#input-help').textContent = value ? '답변을 기다리는 동안 다음 질문을 적어 둘 수 있습니다.' : touchKeyboard() ? '전송 버튼으로 보내기 · Enter 줄바꿈' : 'Enter 전송 · Shift + Enter 줄바꿈';
}
function lock() {
  saveDraft();
  viewVersion++; historyVersion++; authenticated = false; stopStream(); setBusy(false);
  turns.clear(); $('#messages').replaceChildren(); $('#history').replaceChildren(); $('#recent-history').replaceChildren();
  $('#welcome').hidden = false; $('#logout').hidden = true;
  for (const id of ['#feedback', '#recent', '#reports-dialog']) if ($(id).open) $(id).close();
  if (!$('#access').open) $('#access').showModal();
}
async function unlock(info = {}) {
  authenticated = true; $('#access').close(); $('#access-token').value = ''; $('#access-status').textContent = ''; $('#logout').hidden = false; setBusy(true);
  runtimeMock = info.mode === 'mock'; mockMode = runtimeMock; protocolFixtureMode = false; $('#protocol-fixture').checked = false;
  $('#runtime-notice').hidden = !mockMode;
  $('#runtime-notice').textContent = '정해진 답변과 가상 상품으로 연동을 확인합니다. 실제 모델·검색 API는 호출하지 않습니다.';
  $('#recipient-panel').hidden = mockMode;
  if (mockMode) {
    $('#welcome-description').textContent = '시나리오를 선택해 전송하세요. 일반 질문에는 같은 답변과 테스트 카드 3개가 나옵니다.';
    $('.examples').replaceChildren();
    for (const scenario of info.scenarios || []) { const button = el('button', '', scenario.label); button.type = 'button'; button.dataset.question = scenario.message; $('.examples').append(button); }
  }
  try {
    await loadRecipients();
    setFixtureMode(false);
  } catch (e) { lock(); throw e; }
  const existing = new URL(window.location.href).searchParams.get('conversation');
  if (existing && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(existing)) { setBusy(false); await openConversation(existing); }
  else { cid = null; routeConversation(); restoreRecipient(); restoreDraft(); await loadHistory(); setBusy(false); }
  if (pendingTurn && cid !== pendingTurn.cid) { setBusy(true); $('#status').textContent = '접수 확인 필요'; notice('이전에 보낸 질문의 상태를 확인하지 못했습니다. 답변 이어받기를 눌러 확인할 수 있습니다.', true); }
  if (!touchKeyboard()) $('#message').focus();
}
async function loadHistory() {
  const version = ++historyVersion, view = viewVersion, items = await api('/demo/conversations');
  if (!authenticated || version !== historyVersion || view !== viewVersion) return;
  for (const selector of ['#history', '#recent-history']) {
    const list = $(selector); list.replaceChildren();
    for (const item of items) {
      const button = el('button', item.id === cid ? 'active' : '', item.title);
      button.setAttribute('aria-current', item.id === cid ? 'true' : 'false');
      button.onclick = () => { if (!busy) { $('#recent').close(); openConversation(item.id); } };
      list.append(button);
    }
    if (!items.length) list.append(el('p', 'empty-history', '아직 저장된 대화가 없습니다.'));
  }
}
function routeConversation() {
  const url = new URL(window.location.href);
  if (cid) url.searchParams.set('conversation', cid); else url.searchParams.delete('conversation');
  window.history.replaceState(null, '', url.pathname + url.search);
  $('#conversation-title').textContent = cid ? '저장된 대화' : '새 대화';
}
function refreshHistory() { loadHistory().catch(e => { if (authenticated) notice(e.message, true); }); }
function createTurn(t) {
  let ui = turns.get(t.turnId); if (ui) return ui;
  $('#welcome').hidden = true;
  const root = el('article', 'turn'), heading = el('div', 'answer-header'), state = el('span', 'turn-state', '접수 중');
  heading.append(el('span', 'answer-label', '답변'), state); root.append(el('div', 'question', t.message), heading);
  const answer = el('div', 'answer'), progress = el('div', 'progress'), cards = el('div', 'cards'), error = el('div', 'error'), actions = el('div', 'turn-actions');
  root.append(progress, answer, cards, error, actions); $('#messages').append(root);
  root.setAttribute('data-status', t.status || 'PROCESSING');
  ui = {root, answer, progress, cards, error, actions, state, last: 0, t}; turns.set(t.turnId, ui); return ui;
}
function render(t) {
  const ui = createTurn(t); ui.reveal?.cancel(); ui.reveal = null; ui.t = t; ui.last = t.lastEventId;
  ui.root.setAttribute('data-status', t.status);
  ui.state.textContent = {PROCESSING: '생성 중', COMPLETED: '완료', FAILED: '실패', CANCELLED: '중지됨'}[t.status] || '접수 중';
  ui.state.className = 'turn-state' + (t.status === 'FAILED' ? ' failed' : '');
  ui.raw = t.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n'); answerText(ui.answer, ui.raw);
  ui.progress.textContent = t.status === 'PROCESSING' ? (t.progressMessage || '답변 생성 중') : '';
  ui.error.textContent = t.error?.message || '';
  if (t.status !== 'PROCESSING') {
    ui.actions.replaceChildren(); const feedback = el('button', '', '문제 신고');
    feedback.onclick = () => { feedbackTurn = t.turnId; feedbackSubmission = crypto.randomUUID(); $('#feedback-status').textContent = '문제와 기대한 동작을 구체적으로 적어 주세요.'; $('#verdict').value = 'issue'; $('#report-category').value = t.status === 'FAILED' ? 'error' : 'quality'; $('#note').value = ''; $('#feedback').showModal(); };
    if (ui.raw) {
      const copy = el('button', '', '답변 복사'); copy.setAttribute('aria-live', 'polite');
      copy.onclick = async () => { try { await navigator.clipboard.writeText(ui.raw); copy.textContent = '복사됨'; } catch { copy.textContent = '복사 실패 · 다시 시도'; } };
      copy.onblur = () => { copy.textContent = '답변 복사'; }; ui.actions.append(copy);
    }
    ui.actions.append(feedback);
    const report = el('a', '', '턴 보고서 다운로드'); report.href = `/demo/conversations/${cid}/turns/${t.turnId}/report?format=text`; ui.actions.append(report);
    if (['FAILED', 'CANCELLED'].includes(t.status)) { const retry = el('button', '', '질문 수정'); retry.onclick = () => editQuestion(t.message); ui.actions.append(retry); }
    loadCards(t.turnId);
  }
  return ui;
}
async function loadCards(tid) {
  const ui = turns.get(tid), conversation = cid; if (!ui) return;
  try {
    const data = await api(`/demo/conversations/${conversation}/turns/${tid}/products`);
    if (!authenticated || cid !== conversation || turns.get(tid) !== ui) return;
    ui.cards.replaceChildren(); const reasons = new Map(ui.t.blocks.flatMap(b => b.products || []).map(p => [p.productId, p.reason]));
    for (const p of data.products) {
      if (!reasons.has(p.productId)) continue;
      const card = el('div', 'card');
      if (p.image) { const img = el('img'); img.src = /^\/media\/[A-Za-z0-9._-]+$/.test(p.image) && !p.image.includes('..') ? p.image : '/demo/images/' + p.image.split('/').pop(); img.alt = p.name; img.loading = 'lazy'; img.onerror = () => img.remove(); card.append(img); }
      const body = el('div', 'card-body'); body.append(el('div', 'card-brand', p.brand || ''), el('h3', '', p.name), el('div', 'price', Number(p.price).toLocaleString() + '원'), el('p', 'reason', reasons.get(p.productId))); card.append(body); ui.cards.append(card);
    }
    if (data.products.length && reasons.size) { ui.cards.append(el('div', 'snapshot', mockMode ? '가상 상품이며 실제 상품 ID와 연결되지 않습니다.' : '수집 시점의 가격 · 재고와 배송은 판매처에서 확인')); scroll(); }
  } catch (e) { if (e.status !== 401 && turns.get(tid) === ui) ui.error.textContent = '상품 정보를 불러오지 못했습니다. 답변은 저장되어 있습니다.'; }
}
function finishTurn(t) {
  stopStream(false); const version = viewVersion, ui = turns.get(t.turnId);
  const finish = () => { if (!authenticated || version !== viewVersion) return; render(t); setBusy(false); notice(); refreshHistory(); scroll(); };
  if (ui?.reveal) ui.reveal.finish(finish); else finish();
}
function appendAnswer(ui, delta) {
  if (mockMode) {
    ui.raw = (ui.raw || '') + delta;
    answerText(ui.answer, ui.raw); scroll(); return;
  }
  if (!ui.reveal) {
    const text = document.createTextNode(ui.raw || ''); ui.answer.replaceChildren(text);
    ui.reveal = window.ChatReveal.create({initial: ui.raw || '', write: value => { text.textContent += value; }, changed: () => scroll()});
  }
  ui.raw = (ui.raw || '') + delta; ui.reveal.append(delta);
}
function showPending() {
  if (!pendingTurn || cid !== pendingTurn.cid) return;
  const ui = createTurn({turnId: pendingTurn.tid, message: pendingTurn.message});
  ui.progress.textContent = '질문 접수 여부 확인 중';
  setBusy(true); $('#status').textContent = '접수 확인 필요';
  notice('질문이 접수됐는지 확인하지 못했습니다. 답변 이어받기로 같은 질문의 상태를 확인할 수 있습니다.', true);
}
async function subscribe(tid, conversation = cid) {
  const version = ++streamVersion, view = viewVersion;
  if (streamController) streamController.abort(); active = tid; notice(); setBusy(true);
  const ui = turns.get(tid), current = () => authenticated && cid === conversation && view === viewVersion && version === streamVersion;
  for (let retry = 0; retry < 4; retry++) {
    try {
      streamController = new AbortController();
      const response = await fetch(`/demo/conversations/${conversation}/turns/${tid}/events?afterEventId=${ui.last}`, {signal: streamController.signal});
      await ChatHTTP.check(response, lock); if (!current()) return;
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      try {
        while (true) {
          const {value, done} = await reader.read(); if (!current()) return; if (done) break;
          buffer += decoder.decode(value, {stream: true}); let split;
          while ((split = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, split); buffer = buffer.slice(split + 2); let id, event, data;
            for (const line of frame.split('\n')) { if (line.startsWith('id:')) id = Number(line.slice(3)); if (line.startsWith('event:')) event = line.slice(6).trim(); if (line.startsWith('data:')) data = JSON.parse(line.slice(5)); }
            if (!id || id <= ui.last) continue; ui.last = id;
            if (event === 'progress') ui.progress.textContent = data.message;
            if (event === 'answer.delta') { ui.progress.textContent = ''; appendAnswer(ui, data.delta); }
            if (event === 'turn.finished') { finishTurn(data); return; }
            scroll();
          }
        }
      } finally { reader.cancel().catch(() => {}); }
      const snapshot = await api(`/demo/conversations/${conversation}`); if (!current()) return;
      const t = snapshot.data.turns.find(t => t.turnId === tid);
      if (t) { if (t.status !== 'PROCESSING') { finishTurn(t); return; } render(t); }
    } catch (e) { if (e.name === 'AbortError' || e.status === 401 || !current()) return; notice('연결을 복구하고 있어요…'); }
    await new Promise(resolve => setTimeout(resolve, 1000 * (retry + 1))); if (!current()) return;
  }
  $('#status').textContent = '연결 끊김'; notice('답변 연결이 끊겼습니다. 답변 이어받기를 누르면 저장된 내용부터 계속 받을 수 있습니다.', true);
}
async function openConversation(id) {
  if (busy && cid !== id && pendingTurn?.cid !== id) return;
  saveDraft();
  const version = ++viewVersion; stopStream(); notice(); setBusy(true);
  try {
    const result = await api('/demo/conversations/' + id); if (!authenticated || version !== viewVersion) return;
    cid = id; routeConversation(); setFixtureMode(Boolean(result.protocolFixture)); setRecipient(result.recipient); restoreDraft(); turns.clear(); $('#messages').replaceChildren();
    $('#welcome').hidden = result.data.turns.length > 0; for (const t of result.data.turns) render(t);
    if (pendingTurn?.cid === id && result.data.turns.some(t => t.turnId === pendingTurn.tid)) rememberPending(null);
    const processing = result.data.turns.find(t => t.status === 'PROCESSING');
    if (pendingTurn?.cid === id) showPending();
    else if (processing) subscribe(processing.turnId, id);
    else setBusy(false);
    refreshHistory(); scroll(true);
  } catch (e) {
    if (version !== viewVersion || !authenticated) return;
    if ([404, 409].includes(e.status)) {
      if (localStorage.getItem('chatConversation') === id) localStorage.removeItem('chatConversation');
      if (e.status === 404 && pendingTurn?.cid === id) {
        cid = id; routeConversation(); setRecipient(recipientOptions.find(item => item.key === pendingTurn.recipientKey)); restoreDraft(); turns.clear(); $('#messages').replaceChildren(); showPending(); return;
      }
      cid = null; routeConversation(); restoreRecipient(); setBusy(Boolean(pendingTurn)); notice(e.message, Boolean(pendingTurn)); refreshHistory();
    } else { setBusy(Boolean(pendingTurn)); notice(e.message, e.status !== 401); }
  }
}
async function deliverPending() {
  const request = pendingTurn, version = viewVersion; if (!request || !authenticated) return;
  setBusy(true); notice();
  try {
    // This read/create operation also recovers a turn whose acknowledgement was lost.
    const snapshot = await api('/demo/conversations/' + request.cid, 'PUT', {recipientKey: request.recipientKey || 'none', ...(request.protocolFixture ? {protocolFixture:true} : {})});
    if (!authenticated || version !== viewVersion || cid !== request.cid) return;
    setFixtureMode(Boolean(snapshot.protocolFixture)); setRecipient(snapshot.recipient);
    const existing = snapshot.data.turns.find(t => t.turnId === request.tid);
    const t = existing || (await api(`/demo/conversations/${request.cid}/turns/${request.tid}`, 'PUT', {message: request.message, ...(request.model ? {model: request.model} : {})})).data;
    if (!authenticated || version !== viewVersion || cid !== request.cid) return;
    if (pendingTurn === request) rememberPending(null);
    if ($('#message').value.trim() === request.message) { $('#message').value = ''; updateComposer(); }
    if (t.status === 'PROCESSING') { render(t); subscribe(t.turnId, request.cid); } else finishTurn(t);
    scroll();
  } catch (e) {
    if (!authenticated || version !== viewVersion || cid !== request.cid) return;
    if ([400, 404, 409, 413, 422, 429].includes(e.status)) {
      if (pendingTurn === request) rememberPending(null);
      const ui = turns.get(request.tid); if (ui) { ui.root.remove(); turns.delete(request.tid); }
      $('#welcome').hidden = turns.size > 0; setBusy(false); notice(e.message);
    } else showPending();
  }
}
async function send(text) {
  if (!authenticated) { lock(); return; }
  if (busy || modelApplying || pendingTurn || !text.trim()) return;
  if (Array.from(text.trim()).length > 200) { notice('질문은 앞뒤 공백을 제외하고 200자까지 입력할 수 있습니다.'); return; }
  if (!cid) { cid = crypto.randomUUID(); try { sessionStorage.removeItem('chatDraft:new'); } catch {} saveDraft(); } routeConversation();
  rememberPending({cid, tid: crypto.randomUUID(), message: text.trim(), recipientKey: protocolFixtureMode ? 'none' : selectedRecipient?.key || 'none', ...(protocolFixtureMode ? {protocolFixture:true} : {})});
  createTurn({turnId: pendingTurn.tid, message: pendingTurn.message}).progress.textContent = '질문 전송 중'; scroll(true);
  await deliverPending();
}
$('#composer').onsubmit = e => { e.preventDefault(); if (!composing) send($('#message').value); };
$('#message').oninput = () => updateComposer();
$('#message').oncompositionstart = () => { composing = true; };
$('#message').oncompositionend = () => { composing = false; };
$('#message').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !composing && !e.isComposing && e.keyCode !== 229 && !touchKeyboard()) { e.preventDefault(); send(e.target.value); } };
$('#new').onclick = () => {
  if (busy || pendingTurn || !authenticated) return;
  saveDraft(); viewVersion++; stopStream(); cid = null; localStorage.removeItem('chatConversation'); routeConversation(); setFixtureMode($('#protocol-fixture').checked); restoreRecipient(); restoreDraft(); turns.clear(); $('#messages').replaceChildren(); $('#welcome').hidden = false; notice(); scroll(true); refreshHistory(); $('#message').focus();
};
$('#recipient').onchange = () => { if (!busy && !cid && authenticated) setRecipient(recipientOptions.find(item => item.key === $('#recipient').value), true); };
$('#home').onclick = e => { e.preventDefault(); $('#new').onclick(); };
$('.examples').onclick = e => { if (e.target.tagName === 'BUTTON') editQuestion(e.target.dataset.question); };
$('#scroll').onscroll = () => { const s = $('#scroll'); followLatest = s.scrollHeight - s.scrollTop - s.clientHeight < 48; $('#latest').hidden = followLatest; };
$('#latest').onclick = () => scroll(true);
if (typeof ResizeObserver !== 'undefined') {
  const observer = new ResizeObserver(() => { updateComposer(false); scroll(); });
  observer.observe($('#messages')); observer.observe($('#scroll'));
}
$('#stop').onclick = async () => {
  if (!active) return;
  const conversation = cid, tid = active, version = viewVersion; $('#stop').disabled = true;
  try {
    const result = await api(`/demo/conversations/${conversation}/turns/${tid}/cancellation`, 'PUT');
    if (!authenticated || version !== viewVersion || cid !== conversation || active !== tid) return;
    if (result.data.status !== 'PROCESSING') finishTurn(result.data); else render(result.data);
  } catch (e) { if (authenticated && version === viewVersion) notice(e.message, true); }
  finally { $('#stop').disabled = false; }
};
$('#feedback-close').onclick = () => $('#feedback').close();
$('#feedback-form').onsubmit = async e => {
  e.preventDefault();
  const note = $('#note').value.trim();
  if ((!feedbackTurn || $('#verdict').value !== 'helpful') && (note.length < 3 || note.length > 1500)) { $('#feedback-status').textContent = '문제와 기대한 동작을 3~1500자로 적어 주세요.'; $('#note').focus(); return; }
  try { await api(feedbackTurn ? `/demo/conversations/${cid}/turns/${feedbackTurn}/feedback` : `/demo/conversations/${cid}/reports`, 'POST', {submissionId: feedbackSubmission, verdict: $('#verdict').value, note: $('#note').value, reporterName: $('#reporter').value, category: $('#report-category').value}); $('#feedback-status').textContent = '신고를 저장했습니다.'; }
  catch (e) { $('#feedback-status').textContent = e.message; }
};
$('#access').addEventListener('cancel', e => e.preventDefault());
$('#access-form').onsubmit = async e => {
  e.preventDefault(); $('#access-submit').disabled = true; $('#access-status').textContent = '확인 중…';
  try { const info = await api('/demo/session', 'POST', {token: $('#access-token').value}); await unlock(info); }
  catch (e) { lock(); $('#access-status').textContent = e.message; }
  finally { $('#access-token').value = ''; $('#access-submit').disabled = false; }
};
$('#logout').onclick = async () => { try { await api('/demo/logout', 'POST'); lock(); } catch (e) { notice(e.message); } };
$('#history-toggle').onclick = () => { if (!busy && authenticated) $('#recent').showModal(); };
$('#recent-close').onclick = () => $('#recent').close();
$('#reconnect').onclick = async () => {
  const button = $('#reconnect'); button.disabled = true;
  try {
    if (pendingTurn) { if (cid !== pendingTurn.cid) await openConversation(pendingTurn.cid); if (pendingTurn) await deliverPending(); }
    else if (active && turns.has(active)) await subscribe(active, cid);
    else { if (cid) await openConversation(cid); else await loadHistory(); }
  } catch (e) { notice(e.message, e.status !== 401); }
  finally { button.disabled = false; }
};
(async () => { setBusy(false); try { const info = await api('/demo/session'); await unlock(info); } catch (e) { lock(); $('#access-status').textContent = e.status === 401 ? '' : e.message; } })();

function setFixtureMode(value) {
  protocolFixtureMode = value; mockMode = runtimeMock || value;
  $('#protocol-fixture').checked = value;
  $('#recipient-panel').hidden = mockMode;
  $('#runtime-notice').hidden = !mockMode;
  $('#runtime-notice').textContent = value ? '정해진 답변과 가상 상품으로 연동을 확인합니다. 실제 모델·검색 API는 호출하지 않습니다.' : '정해진 답변과 가상 상품으로 연동을 확인합니다. 실제 모델·검색 API는 호출하지 않습니다.';
  $('#mock-controls').hidden = !mockMode;
  if (mockMode) loadMockScenarios().catch(e => { if (authenticated && mockMode) notice(e.message); });
  if (value) setRecipient(null);
  setBusy(busy);
}
$('#protocol-fixture').onchange = () => { if (!cid && !busy && authenticated) setFixtureMode($('#protocol-fixture').checked); };
let mockScenarioLoad = null;
function loadMockScenarios() {
  if (!mockScenarioLoad) mockScenarioLoad = api('/demo/test-scenarios').then(data => {
    $('#mock-scenario').replaceChildren(new Option('시나리오 선택', ''));
    for (const scenario of data.scenarios || []) $('#mock-scenario').append(new Option(scenario.label, scenario.message));
  }).catch(e => { mockScenarioLoad = null; throw e; });
  return mockScenarioLoad;
}
$('#mock-scenario').onchange = () => {
  if (mockMode && !busy && authenticated && $('#mock-scenario').value) editQuestion($('#mock-scenario').value);
};
$('#mock-resume').onclick = async () => {
  if (!mockMode || !active || !authenticated) return;
  const tid = active, conversation = cid;
  $('#mock-resume').disabled = true;
  try { await subscribe(tid, conversation); }
  finally { $('#mock-resume').disabled = false; }
};
$('#reports-close').onclick = () => $('#reports-dialog').close();
$('#my-reports').onclick = async () => {
  try {
    const list = $('#reports-list'); list.replaceChildren();
    let cursor=null;
    do {
    const data = await api('/demo/reports'+(cursor?'?cursor='+encodeURIComponent(cursor):''));
    for (const r of data.reports) {
      const item = el('article', 'report-item'); item.append(el('h3', '', r.payload.category || 'QA'), el('p', '', r.payload.note || ''), el('p', '', r.status + ' · ' + r.created));
      const detail = el('details'); detail.append(el('summary', '', '연결한 턴 확인'));
      for (const t of r.context.turns || []) { detail.append(el('p', '', '사용자: ' + t.message)); for (const b of t.blocks) if (b.type === 'text') detail.append(el('p', '', '답변: ' + b.text)); }
      item.append(detail); list.append(item);
    }
    cursor=data.nextCursor;
    } while(cursor);
    if (!list.childElementCount) list.append(el('p', '', '저장한 신고가 없습니다.'));
    $('#reports-dialog').showModal();
  } catch(e) { notice(e.message); }
};

$('#report-session').onclick = async () => {
 if (!authenticated || busy || pendingTurn) return;
 if (!cid) {
  const version=viewVersion;cid=crypto.randomUUID();routeConversation();setBusy(true);
  try {
   const snapshot=await api('/demo/conversations/'+cid,'PUT',{recipientKey:protocolFixtureMode?'none':selectedRecipient?.key||'none',...(protocolFixtureMode?{protocolFixture:true}:{})});
   if (!authenticated || version!==viewVersion) return;
   setFixtureMode(Boolean(snapshot.protocolFixture));setRecipient(snapshot.recipient);refreshHistory();
  } catch(e) {notice(e.message);return;}
  finally {if (authenticated && version===viewVersion) setBusy(false);}
 }
 feedbackTurn=null;feedbackSubmission=crypto.randomUUID();$('#verdict').value='issue';$('#feedback-status').textContent='대화의 최근 최대 5개 턴을 연결합니다. 답변이 없는 대화도 신고할 수 있습니다.';$('#note').value='';$('#feedback').showModal();
};
