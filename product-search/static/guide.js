import {parseJSON, stringifyJSON} from './json.js';
const $ = selector => document.querySelector(selector);
const base = location.origin;
let language = 'curl';
let lastSearch = null;
let toastTimer;
$('#base-url').textContent = base;
const quoteShell = value => "'" + value.replaceAll("'", "'\\''") + "'";
function payload() {
  const value = parseJSON($('#request-body').value);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw Error('요청 본문은 JSON 객체여야 합니다.');
  return value;
}
function renderExample() {
  try {
    payload();
    const json = $('#request-body').value.trim();
    const source = $('#request-source').value;
    const url = `${base}/v1/search`;
    const snippets = {
      curl: `curl --max-time 30 --fail-with-body ${quoteShell(url)} \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Search-Source: ${source}' \\\n  --data-raw ${quoteShell(json)}`,
      python: `# pip install httpx\nimport json\nimport httpx\n\npayload = json.loads(${JSON.stringify(json)})\n\nwith httpx.Client(timeout=30.0) as client:\n    response = client.post(\n        ${JSON.stringify(url)},\n        headers={"X-Search-Source": "${source}"},\n        json=payload,\n    )\n    response.raise_for_status()\n    result = response.json()\n    print(result["hits"])`,
      javascript: `const response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: {\n    "Content-Type": "application/json",\n    "X-Search-Source": "${source}"\n  },\n  signal: AbortSignal.timeout(30000),\n  body: ${JSON.stringify(json)}\n});\nconst text = await response.text();\nif (!response.ok) {\n  throw new Error("HTTP " + response.status + ": " + text);\n}\n// BIGINT ID를 보존하려면 lossless JSON 파서를 사용하세요.\n// JSON.parse(text)는 큰 정수를 반올림할 수 있습니다.\nconsole.log(text);`
    };
    $('#request-code').textContent = snippets[language];
    $('#copy-code').disabled = false;
  } catch {
    $('#request-code').textContent = '올바른 JSON 객체를 입력하면 호출 예제가 표시됩니다.';
    $('#copy-code').disabled = true;
  }
}
function message(text) {
  clearTimeout(toastTimer);
  $('#toast').textContent = text;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => $('#toast').hidden = true, 2200);
}
function setBusy(busy) {
  $('#run-request').disabled = busy;
  $('#fetch-details').disabled = busy || !lastSearch?.hits?.length;
}
async function execute(path, body) {
  setBusy(true);
  $('#playground-error').hidden = true;
  $('#live-response').open = true;
  $('#response-status').textContent = `POST ${path} · 실행 중`;
  $('#response-json').textContent = '응답을 기다리는 중…';
  const start = performance.now();
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Search-Source': $('#request-source').value},
      body: typeof body === 'string' ? body : stringifyJSON(body), signal: AbortSignal.timeout(30000)
    });
    const raw = await response.text();
    let data;
    try { data = parseJSON(raw); } catch { data = raw; }
    $('#response-status').textContent = `POST ${path} · HTTP ${response.status} · ${Math.round(performance.now() - start)}ms`;
    $('#response-json').textContent = typeof data === 'string' ? data : stringifyJSON(data, 2);
    if(!response.ok && data && typeof data==='object'){
      const details=(data.error?.issues??[]).map(issue=>`${issue.field?issue.field+': ':''}${issue.message}`).join(' / ');
      $('#playground-error').textContent=`${data.error?.code?'['+data.error.code+'] ':''}${data.message||'요청에 실패했습니다.'}${details?' · '+details:''}`;
      $('#playground-error').hidden=false;
    }
    if (path === '/v1/search') lastSearch = response.ok ? data : null;
  } catch (error) {
    $('#response-status').textContent = 'HTTP 응답을 받지 못했습니다';
    $('#response-json').textContent = error.name === 'TimeoutError' ? '30초 내에 응답하지 않았습니다. 서버 상태를 확인하세요.' : '서버에 연결할 수 없습니다. 서버 주소와 실행 상태를 확인하세요.';
  } finally { setBusy(false); }
}
$('#request-body').addEventListener('input', () => {lastSearch = null; $('#fetch-details').disabled = true; renderExample();});
$('#request-source').addEventListener('change', renderExample);
document.querySelectorAll('[data-language]').forEach(button => button.addEventListener('click', () => {
  language = button.dataset.language;
  document.querySelectorAll('[data-language]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  renderExample();
}));
$('#copy-code').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('#request-code').textContent); message('호출 예제를 복사했습니다.'); }
  catch { message('복사하지 못했습니다. 코드 영역의 텍스트를 직접 선택해 주세요.'); }
});
$('#run-request').addEventListener('click', () => {
  let body;
  try { payload(); body = $('#request-body').value; } catch {
    $('#playground-error').textContent = 'JSON 형식을 확인하세요. 요청 본문은 중괄호로 감싼 객체여야 합니다.';
    $('#playground-error').hidden = false; return;
  }
  lastSearch = null;
  execute('/v1/search', body);
});
$('#fetch-details').addEventListener('click', () => {
  if (lastSearch?.hits?.length) execute('/v1/products', {ids:lastSearch.hits.map(hit => hit.productId),snapshotId:lastSearch.snapshotId});
});
async function loadInfo() {
  const outcomes = await Promise.allSettled(['/readyz', '/v1/metadata'].map(async path => {
    const response = await fetch(path, {signal:AbortSignal.timeout(10000)});
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    return parseJSON(await response.text());
  }));
  const [ready, metadata] = outcomes;
  $('#server-status').textContent = ready.status === 'fulfilled' && ready.value.status === 'ready' ? `연결됨 · ${ready.value.products.toLocaleString('ko-KR')}개 상품` : '서버 연결을 확인하세요';
  if(ready.status==='fulfilled')$('#server-status').title=ready.value.exportGeneratedAt?`Backend export 시각: ${ready.value.exportGeneratedAt}`:'Backend export 시각 정보 없음';
  $('#metadata-json').textContent = metadata.status === 'fulfilled' ? stringifyJSON(metadata.value, 2) : '메타데이터를 불러오지 못했습니다. /v1/metadata 또는 서버 상태를 확인하세요.';
}
renderExample();
loadInfo();
