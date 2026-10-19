const form = document.getElementById('login-form');
const password = document.getElementById('admin-password');
const button = document.getElementById('login-submit');
const note = document.getElementById('login-note');
form.addEventListener('submit', async event => {
  event.preventDefault(); if (button.disabled) return;
  button.disabled = true; note.textContent = '확인 중…';
  const body = JSON.stringify({password: password.value}); password.value = '';
  try {
    const response = await fetch('/api/admin/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body, credentials: 'same-origin', cache: 'no-store', mode: 'same-origin', redirect: 'error'});
    if (response.ok) { location.replace('/?view=dashboard'); return; }
    note.textContent = response.status === 429 ? '시도가 많습니다. 5분 후 다시 입력해 주세요.'
      : response.status === 503 ? '관리자 로그인이 아직 설정되지 않았습니다. 운영자에게 문의해 주세요.'
      : '로그인하지 못했습니다. 비밀번호와 접속 주소를 확인해 주세요.';
  } catch { note.textContent = '연결을 확인한 뒤 다시 시도해 주세요.'; }
  finally { button.disabled = false; password.focus(); }
});
