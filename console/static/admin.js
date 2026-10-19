// Only the CSRF token is held in memory. The session cookie is HttpOnly.
let csrf = null;
const configuredCredentials = new Map();
function forgetConversation(){try{for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('console-chat/'))sessionStorage.removeItem(key);}}catch{}}
export function setSession(value) { csrf = value; }
export function scheduleSessionExpiry(seconds, now = Date.now, schedule = setTimeout, expired = () => {forgetConversation();location.replace('/login');}) {
  const deadline = now() + Math.max(0, seconds) * 1000;
  function check() {
    const remaining = deadline - now();
    if (remaining <= 0) expired();
    else schedule(check, Math.min(remaining, 2147483647));
  }
  check();
}
export async function adminFetch(path, options = {}, fetcher = globalThis.fetch.bind(globalThis)) {
  const headers = new Headers(options.headers || {});
  if (!['GET', 'HEAD'].includes(options.method || 'GET')) headers.set('X-CSRF-Token', csrf || '');
  const response = await fetcher(path, {...options, headers, credentials: 'same-origin', cache: 'no-store', mode: 'same-origin', redirect: 'error'});
  if (response.status === 401 && typeof location !== 'undefined') {forgetConversation();location.replace('/login');}
  return response;
}
export async function initializeAdmin() {
  const response = await adminFetch('/api/admin/session');
  if (!response.ok) { location.replace('/login'); throw new Error('Administrator login required'); }
  const session = await response.json(); setSession(session.csrf);
  scheduleSessionExpiry(session.expiresIn);
  const logout = document.getElementById('admin-logout');
  logout.onclick = async () => {
    if (logout.disabled) return;
    logout.disabled = true;
    try {
      let result = await adminFetch('/api/admin/logout', {method: 'POST'});
      if (result.status === 403 && (await result.json()).detail === 'CSRF_REJECTED') {
        // Another tab can replace the shared cookie while this tab keeps its CSRF.
        const current = await adminFetch('/api/admin/session');
        if (current.ok) {
          setSession((await current.json()).csrf);
          result = await adminFetch('/api/admin/logout', {method: 'POST'});
        } else result = current;
      }
      if (result.ok || result.status === 401) { setSession(null);forgetConversation();location.replace('/login'); }
      else throw new Error('Logout unconfirmed');
    } catch {
      const message = '로그아웃을 확인하지 못했습니다. 다시 시도해 주세요.';
      document.getElementById('provider-note').textContent = message;
      const toast = document.getElementById('toast');
      toast.textContent = message; toast.classList.remove('hidden');
      setTimeout(() => toast.classList.add('hidden'), 4500);
    } finally { logout.disabled = false; }
  };
  await Promise.all([
    credentialForm('provider', '/api/admin/provider-key/chat', '키', 'Chat 전용입니다. Profiler 키로 대체하지 않습니다.'),
    credentialForm('profiler', '/api/admin/provider-key/profiler', '키', 'Profiler 전용입니다. Chat 키로 대체하지 않습니다.'),
    credentialForm('backend', '/api/admin/backend-token', '토큰', 'Chat·Profiler가 함께 사용합니다. 백엔드에도 같은 값을 설정하세요. Search 내부 토큰은 별도입니다.'),
  ]);
  return session;
}
async function credentialForm(id, endpoint, label, help) {
  const input = document.getElementById(`${id}-key`);
  const button = document.getElementById(`${id}-save`);
  const note = document.getElementById(`${id}-note`);
  const confirmation = document.getElementById(`${id}-confirm`);
  let state = null;
  async function refresh() {
    const r = await adminFetch(endpoint);
    if (!r.ok) throw new Error('Unavailable');
    state = await r.json(); input.disabled = button.disabled = !state.writable;
    configuredCredentials.set(id, state.configured === true);
    document.getElementById('credential-setup-notice').hidden =
      ['provider', 'profiler', 'backend'].every(kind => configuredCredentials.get(kind) === true);
    document.getElementById('credential-environment').textContent = `현재 환경: ${state.environment || '미확인'}`;
    if (confirmation) {
      confirmation.checked = false;
      confirmation.closest('label').hidden = !state.migrationRequired;
      confirmation.required = !!state.migrationRequired;
    }
    const application = Object.entries(state.application || {}).map(([service, value]) =>
      `${service === 'chat' ? 'Chat' : 'Profiler'} ${value === 'observed' ? '읽기 확인' : value === 'pending' ? '적용 대기' : '적용 미확인'}`).join(' · ');
    document.getElementById(`${id}-status`).textContent = state.configured ? `${label} ${state.source === 'deployment' ? '배포 설정 사용 중' : '저장됨'} · ${application || '적용 미확인'}` : `${label} 없음 또는 읽기 불가`;
    note.textContent = state.state === 'source_conflict' ? `${label} 설정이 두 곳에 있어 사용할 수 없습니다. 서버 실행 설정을 확인해 주세요. 기존 값은 보존됩니다.`
      : state.migrationRequired ? `이전 파일은 보존됩니다. 자동 재사용하지 않으므로 이 용도에 맞는 ${label}를 입력하고 전환을 확인해 주세요. ${help}`
      : state.mode === 'external' || state.mode === 'aws' ? `${label}를 서버 실행 설정에서 관리하고 있어 여기서는 바꿀 수 없습니다.`
      : !state.writable ? '저장 설정을 사용할 수 없습니다. 운영자에게 문의해 주세요.'
      : `여기서 저장한 값이 배포 초기값보다 우선하며 재시작 후에도 유지됩니다. AWS 원본은 바꾸지 않습니다. 저장한 값은 다시 표시하지 않습니다. 읽기 확인은 모델 연결·잔액 검증이 아닙니다. ${help}`;
  }
  document.getElementById(`${id}-form`).onsubmit = async event => {
    event.preventDefault(); if (button.disabled || !state?.writable) return;
    if (state.migrationRequired && !confirmation?.checked) return;
    button.disabled = true;
    const body = JSON.stringify({key: input.value, revision: state.revision,
      ...(id === 'backend' ? {confirmShared: confirmation?.checked === true} : {confirmMigration: confirmation?.checked === true})}); input.value = '';
    // The write can succeed even if its HTTP response is lost.
    if (id !== 'backend') document.dispatchEvent(new Event('provider-key-changing'));
    try {
      const r = await adminFetch(endpoint, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body});
      if (r.ok) document.dispatchEvent(new Event('admin-settings-saved'));
      await refresh();
      note.textContent = r.ok ? `${label}${label === '키' ? '를' : '을'} 저장했습니다. 연결 검증이나 유료 호출은 하지 않았습니다. ${help}`
        : r.status === 409 ? '설정이 변경되었습니다. 현재 상태를 확인하고 다시 입력해 주세요.'
        : '저장 완료를 확인하지 못했습니다. 현재 상태를 확인한 뒤 다시 입력해 주세요.';
    } catch { state = null; note.textContent = '저장 완료를 확인하지 못했습니다. 새로고침하여 상태를 확인해 주세요.'; }
    finally {
      button.disabled = !state?.writable;
      if (id !== 'backend') document.dispatchEvent(new Event('provider-key-change-finished'));
    }
  };
  try { await refresh(); }
  catch {
    configuredCredentials.set(id, false);
    document.getElementById('credential-setup-notice').hidden = false;
    input.disabled = button.disabled = true; note.textContent = '설정 상태를 읽지 못했습니다. 새로고침하여 확인해 주세요.';
  }
}
