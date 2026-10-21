import {adminFetch} from './admin.js';

const amount = value => typeof value === 'number' && Number.isFinite(value)
  ? '$' + value.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:6}) : '미확인';

export function usageView(data = {}, elapsedSeconds = 0) {
  const stale = typeof data.ageSeconds === 'number' && data.ageSeconds + elapsedSeconds >= (data.staleAfterSeconds || 300);
  const state = data.state === 'failed' ? 'failed' : stale ? 'stale' : data.state;
  const labels = {verified:'조회 확인', unlimited:'키 한도 없음', partial:'일부 값 미확인', stale:'오래된 조회',
    failed:data.checkedAt ? '조회 실패 · 이전 값' : '조회 실패', unconfigured:'키 미설정', unavailable:'키 상태 미확인'};
  const reset = {daily:'매일 UTC', weekly:'매주 월요일 UTC', monthly:'매월 UTC'}[data.limitReset];
  const checked = data.checkedAt && Number.isFinite(Date.parse(data.checkedAt))
    ? new Date(data.checkedAt).toLocaleString('ko-KR', {hour12:false}) : '아직 확인하지 못했습니다';
  return {state:state || 'unknown', status:labels[state] || '조회 전', usage:amount(data.usageUsd),
    limit:data.limitState === 'unlimited' ? '설정 없음' : amount(data.limitUsd),
    remaining:data.limitState === 'unlimited' && data.remainingUsd === null ? '한도 없음' : amount(data.remainingUsd),
    checked, reset:reset ? `한도 초기화: ${reset}` : data.limitReset === 'unknown' ? '초기화 주기 미확인' : '누적 사용액은 전체 기간 기준입니다.'};
}

export function initializeProviderUsage() {
  const button = document.getElementById('refresh-provider-usage');
  let values = {}, receivedAt = performance.now(), sequence = 0, pendingWrites = 0;
  function render() {
    for (const kind of ['chat', 'profiler']) {
      const view = usageView(values[kind], Math.max(0, performance.now() - receivedAt) / 1000);
      for (const field of ['status','usage','limit','remaining','checked','reset'])
        document.getElementById(`usage-${kind}-${field}`).textContent = view[field];
      document.getElementById(`usage-${kind}`).dataset.state = view.state;
    }
  }
  async function refresh() {
    if (pendingWrites) return;
    const current = ++sequence;
    button.disabled = true; button.textContent = '조회 중…';
    try {
      const response = await adminFetch('/api/admin/provider-usage');
      if (!response.ok) throw new Error('Usage unavailable');
      const result = await response.json();
      if (current !== sequence) return;
      values = result; receivedAt = performance.now();
    } catch {
      if (current !== sequence) return;
      for (const kind of ['chat', 'profiler']) values[kind] = {...values[kind], state:'failed'};
    } finally {
      if (current === sequence) { button.disabled = false; button.textContent = '키 한도 조회'; render(); }
    }
  }
  button.onclick = refresh;
  document.addEventListener('provider-key-changing', () => {
    ++pendingWrites;
    ++sequence; values = {}; receivedAt = performance.now();
    button.disabled = true; button.textContent = '키 저장 중…'; render();
  });
  document.addEventListener('provider-key-change-finished', () => {
    pendingWrites = Math.max(0, pendingWrites - 1);
    if (pendingWrites) return;
    // A failed refresh after a credential change must not keep the old key's amounts.
    values = {}; receivedAt = performance.now(); render(); void refresh();
  });
  setInterval(render, 30000); // Age display only; no background provider polling.
  void refresh(); // Never hold up chat controls or their readiness checks.
}
