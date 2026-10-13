'use strict';
const menu = document.querySelector('#guide-menu');
const mobile = window.matchMedia('(max-width: 900px)');
function adaptMenu() { menu.open = !mobile.matches; }
adaptMenu();
mobile.addEventListener('change', adaptMenu);
menu.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
  if (mobile.matches) menu.open = false;
}));
document.querySelectorAll('.copy-code').forEach(button => {
  button.addEventListener('click', async () => {
    const code = button.closest('.code-shell').querySelector('code').textContent;
    try {
      await navigator.clipboard.writeText(code);
      button.textContent = '복사됨';
      document.querySelector('#copy-status').textContent = '코드를 복사했습니다.';
      window.setTimeout(() => { button.textContent = '복사'; }, 1800);
    } catch (_) {
      button.textContent = '텍스트를 선택해 복사';
      document.querySelector('#copy-status').textContent = '자동 복사를 사용할 수 없습니다. 코드 부분을 선택해 복사하세요.';
    }
  });
});
