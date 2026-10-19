"""Private review route and browser retries; all requests stay in ASGI/fake DOM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import httpx
import pytest

from app import main, security


@pytest.fixture
def review_store(monkeypatch):
    class Store:
        def __init__(self):
            self.calls = []

        async def call(self, method, **kwargs):
            self.calls.append((method, kwargs))
            return {"saved": True, "duplicate": False, "reviewId": kwargs["review_id"],
                    "note": kwargs["note"], "created": "2026-10-18T12:00:00+00:00"}

    store = Store()
    security.rate.clear()
    monkeypatch.setattr(main.app.state, "store", store, raising=False)
    return store


def review_path():
    cid, tid, rid = (str(uuid4()) for _ in range(3))
    return f"/admin/api/conversations/{cid}/turns/{tid}/reviews/{rid}", cid, tid, rid


async def login(client):
    response = await client.post("/admin/login", json={"password": os.environ["QA_ADMIN_PASSWORD"]},
                                 headers={"Origin": "https://synthetic"})
    assert response.status_code == 200


@pytest.mark.parametrize("cookie", [None, "forged", "stale"])
async def test_review_requires_current_admin_cookie_even_with_service_token(review_store, cookie, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://synthetic") as client:
        if cookie == "forged":
            client.cookies.set("chat_admin", "forged-cookie")
        elif cookie == "stale":
            await login(client)
            monkeypatch.setenv("QA_ADMIN_PASSWORD", "another-synthetic-admin-password")
        response = await client.put(review_path()[0], json={"note": "합성 평가"},
                                    headers={"Authorization": "Bearer " + main.TOKEN})
    assert response.status_code == 401
    assert review_store.calls == []


async def test_review_route_uses_existing_admin_auth_and_exact_turn_binding(review_store):
    path, cid, tid, rid = review_path()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://synthetic") as client:
        await login(client)
        response = await client.put(path, json={"note": "  실제 검색 근거가 부족하여 판단 보류\n추가 확인 필요  "},
                                    headers={"Origin": "https://synthetic"})
    assert response.status_code == 200 and response.json()["saved"]
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert review_store.calls == [("admin_turn_review", {
        "cid": cid, "tid": tid, "review_id": rid, "note": "실제 검색 근거가 부족하여 판단 보류\n추가 확인 필요"})]
    assert not any("/admin/" in path for path in main.app.openapi()["paths"])


@pytest.mark.parametrize("headers", [{"Origin": "https://elsewhere.invalid"}, {"Sec-Fetch-Site": "cross-site"}])
async def test_review_rejects_cross_origin_before_storage(review_store, headers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://synthetic") as client:
        await login(client)
        response = await client.put(review_path()[0], json={"note": "합성 평가"}, headers=headers)
    assert response.status_code == 403 and not review_store.calls


@pytest.mark.parametrize("body", [
    {"note": " \t\n "}, {"note": "PRIVATE_NUL_CANARY\x00"}, {"note": "가" * 1501},
    {"note": "PRIVATE_SURROGATE_CANARY\ud800"}, {"note": "합성 평가", "resolved": True},
])
async def test_review_invalid_text_never_reaches_storage_or_response(review_store, body, caplog):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://synthetic") as client:
        await login(client)
        response = await client.put(review_path()[0], content=json.dumps(body).encode(),
                                    headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and not review_store.calls
    assert "PRIVATE_" not in response.text and "PRIVATE_" not in caplog.text


def test_admin_note_browser_retry_identity_and_literal_rendering():
    """Exercise the actual editor after a simulated lost acknowledgement."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the browser controller test")
    script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  set innerHTML(value) { throw new Error('HTML parsing is forbidden for review text'); }
}
const elements = new Map();
const document = {
  createElement: tag => new Element(tag),
  querySelector: selector => { if (!elements.has(selector)) elements.set(selector, new Element('div')); return elements.get(selector); },
  querySelectorAll: () => [],
};
const requests = [], persisted = new Map(); let sequence = 0, loseFirstAck = true;
const sandbox = {document, URLSearchParams, Date, JSON, Object, setTimeout,
  FormData: class { get() { return ''; } }, crypto: {randomUUID: () => 'review-' + ++sequence},
  ChatHTTP: {json: async (path, options = {}) => {
    if (options.method !== 'PUT') return {items: [], nextCursor: null};
    const note = JSON.parse(options.body).note;
    requests.push({path, note});
    if (!persisted.has(path)) persisted.set(path, {reviewId: path.split('/').at(-1), note, created: '2026-10-18T12:00:00Z'});
    if (loseFirstAck) { loseFirstAck = false; throw new Error('Synthetic lost acknowledgement'); }
    return persisted.get(path);
  }}, testTurn: {turnId: 'turn-one', reviews: []}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const find = (node, tag) => node.tagName === tag ? node : node.children.map(child => find(child, tag)).find(Boolean);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  const editor = vm.runInContext('reviewEditor(testTurn, "conversation-one")', sandbox);
  const form = find(editor, 'form'), input = find(editor, 'textarea'), button = find(editor, 'button');
  const literal = '<img src=x onerror="steal()"> 근거 부족';
  input.value = literal;
  await form.onsubmit({preventDefault() {}});
  assert.equal(sandbox.testTurn.reviews.length, 0);
  assert.equal(input.value, literal);
  assert.equal(button.disabled, false);
  await form.onsubmit({preventDefault() {}});
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(persisted.size, 1);
  assert.equal(sandbox.testTurn.reviews.length, 1);
  assert.equal(sandbox.testTurn.reviews[0].note, literal);
  assert.equal(editor.children[1].children[0].children[1].textContent, literal);
  assert.equal(input.value, '');
  input.value = '추가로 확인한 별도 평가';
  await form.onsubmit({preventDefault() {}});
  assert.notEqual(requests[1].path, requests[2].path);
  assert.equal(sandbox.testTurn.reviews.length, 2);
  const trace = value => vm.runInContext('traceMessage(' + JSON.stringify(value) + ')', sandbox);
  assert.equal(trace({}), '추적 저장 상태 미기록(기존 자료)');
  assert.equal(trace({tracePersistence: {version: 1, status: 'recorded'}}), '실행 추적 저장됨');
  assert.equal(trace({tracePersistence: {version: 1, status: 'interrupted'}}), '중단 시점의 실행 추적 저장됨');
  assert.equal(trace({tracePersistence: {version: 1, status: 'not_recorded'}}), '실행 추적 저장 누락');
  assert.equal(trace({tracePersistence: {version: 1, status: 'pending'}}), '실행 추적 저장 대기');
  assert.equal(trace({tracePersistence: {version: 1, status: '__proto__'}}), '추적 저장 상태 확인 필요');
  assert.equal(trace({tracePersistence: {version: 2, status: 'not_recorded'}}), '추적 저장 상태 확인 필요');
  process.stdout.write('admin editor retry and trace states passed\n');
})().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script, str(Path(__file__).resolve().parents[1] / "app/static/admin.js")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
