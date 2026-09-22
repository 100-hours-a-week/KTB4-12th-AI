"""현재 구현된 파이프라인 지도 — 단계(접수 → 슬롯 → 처리 → 콜백)마다 실제로 부르는 함수와, 포트를 거쳐 연결되는 어댑터·바깥.
python3 build_pipeline_map.py → pipeline-map.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
함수 이름은 코드 기준(2026-09-23, 패키지 평탄화 후). 바뀌면 여기도 고친다."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1700, 1076
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:15px;font-weight:700}}.h2{{font-size:13px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12px;fill:#5e6873}}.msg{{font-size:13px}}
.mono{{font-family:Menlo,ui-monospace,monospace;font-size:11px;fill:#3f4a54}}.monob{{font-family:Menlo,ui-monospace,monospace;font-size:11px;font-weight:700;fill:#18212b}}
.small{{font-size:13px;fill:#5e6873}}.num{{font-size:11px;font-weight:700;fill:#fff}}.port{{font-family:Menlo,ui-monospace,monospace;font-size:12px;font-weight:700;fill:#3b6fb6}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.7;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}.write{{fill:none;stroke:#c0392b;stroke-width:1.8;marker-end:url(#w)}}
.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.3;stroke-dasharray:6 4;marker-end:url(#a)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.stage{{fill:#fff;stroke:#3b6fb6;stroke-width:1.6}}.band{{fill:#eef3fa;stroke:#9db0d2;stroke-width:1.2}}.old{{fill:#f7f8fa;stroke:#d5dbe0;stroke-width:1.2}}
.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.ext{{fill:#f7f8fa;stroke:#bcc5cd;stroke-width:1.2}}.v3{{fill:#fffbe6;stroke:#e6d98a;stroke-width:1;stroke-dasharray:4 3}}.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


def box(x, y, w, h, cls_='box', r=4):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls_}"/>')


def path(d, cls_='call'):
    parts.append(f'<path d="{d}" class="{cls_}"/>')


def num(x, y, n, color='#3b6fb6'):
    parts.append(f'<circle cx="{x}" cy="{y}" r="9" fill="{color}"/><text x="{x}" y="{y+4}" text-anchor="middle" class="num">{n}</text>')


def lines(x, y, rows, dy=14):
    """rows: [(text, cls)]"""
    for i, (s, c) in enumerate(rows):
        t(x, y + i * dy, s, c)


t(40, 44, '프로파일링 파이프라인 지도 — 단계마다 부르는 함수와 연결된 것 (v1, 2026-09-23 코드 기준)', 'title')
t(40, 70, '위에서 아래로: 조립 → 요청 한 건의 4단계 → 포트(Protocol) → 어댑터(구현) → 바깥. 파랑 = 호출 · 빨강 = DB에 씀 · 회색 점선 = 조건부/HTTP · 노랑 점선 = v3 자리(미구현). 숫자 = pipeline.profile()의 단계 번호.', 'small')

# ───────────────────── 0. 조립 (main.py lifespan) ─────────────────────
Y0 = 90
box(40, Y0, W - 80, 104, 'band', 6)
t(52, Y0 + 20, '조립 — main.py  lifespan()  (프로세스 시작 1회, app.state에 둠)', 'h')
lines(52, Y0 + 40, [
    ('get_settings() (settings.py, PROFILING_*)  →  FileCatalogReader(CATALOG_FILE)  →  Supervisor(PROFILING_SLOTS=1)  →  HttpBackendPort(BACKEND_BASE_URL, SERVICE_TOKEN, CALLBACK_TIMEOUT_S)', 'mono'),
    ('STORE=db: _connect_db(DATABASE_URL — select 1 · alembic_version 확인, 실패면 RuntimeError로 앱 안 뜸) → DbProfileRunStore(engine) · DbRecipientProfileStore(engine)    |    STORE=memory: MemoryProfileRunStore() · recipient_store=None', 'mono'),
    ('카탈로그 로드 실패 → _NoCatalog 대역 (7.6은 503, 앱은 뜸)    ·    종료: backend.close() · engine.dispose()    ·    GET /health → catalog · store(_store_health) · supervisor.stats()', 'mono'),
    ('오류 봉투: on_validation_error(422→400 INVALID_REQUEST) · on_http_error(401/403/503) · on_unhandled(500) — 모두 _error_response() {message, error:{code, traceId}}', 'mono'),
])

# ───────────────────── 1~4. 요청 한 건 ─────────────────────
TOP, SH = 232, 348
CW, GAP = 385, 25
XS = [40 + i * (CW + GAP) for i in range(4)]
stages = [
    ('1  접수', 'intake.py', 'extract_and_pool()  — HTTP 안에서, 202까지'),
    ('2  실행 슬롯', 'supervisor.py', 'Supervisor.submit() → _run()  — 응답 뒤 백그라운드'),
    ('3  처리', 'pipeline.py', 'run_and_callback() → profile(rq, catalog, store, …)'),
    ('4  콜백·기록', 'intake.py + backend.py', 'run_and_callback() 뒷부분'),
]
for (name, file, fn), x in zip(stages, XS):
    box(x, TOP, CW, SH, 'stage', 6)
    t(x + 12, TOP + 22, name, 'h'); t(x + 12, TOP + 40, file, 'sub'); t(x + 12, TOP + 56, fn, 'mono')
    parts.append(f'<line x1="{x}" y1="{TOP+64}" x2="{x+CW}" y2="{TOP+64}" stroke="#9db0d2"/>')

# 1 접수
x = XS[0]
lines(x + 12, TOP + 84, [
    ('POST /api/internal/v1/ai/profile/extract-and-pool', 'monob'),
    ('① body → ProfileExtractRequest  (schemas.py, camelCase)', 'mono'),
    ('     위반 → 400 INVALID_REQUEST (main.on_validation_error)', 'sub'),
    ('② require_service_token()  Bearer ≠ SERVICE_TOKEN → 401', 'mono'),
    ('③ catalog.active()  없으면 → 503 SERVICE_UNAVAILABLE', 'mono'),
    ('④ rq = pipeline.to_internal(body)   camel → snake', 'mono'),
    ('⑤ supervisor.submit(bg, run_and_callback, rq, catalog,', 'mono'),
    ('        store, backend, POOL_SIZE, recipient_store)', 'mono'),
    ('⑥ 202 SuccessResponse[ProfileAccepted] · PENDING', 'monob'),
    ('', 'mono'),
    ('의존성(Depends): get_catalog · get_store · get_recipient_store', 'sub'),
    ('· get_backend · get_supervisor  ← app.state', 'sub'),
    ('_error() → HTTPException → main.on_http_error 봉투', 'sub'),
    ('7.6 응답 뒤에는 Backend와의 HTTP가 끝난다', 'sub'),
])
# 2 슬롯
x = XS[1]
lines(x + 12, TOP + 84, [
    ('submit(bg, fn, *args)', 'monob'),
    ('  _submitted += 1 · bg.add_task(_run, fn, *args)', 'mono'),
    ('  → FastAPI BackgroundTasks: 응답 전송 뒤 같은 프로세스에서', 'sub'),
    ('_run(fn, *args)', 'monob'),
    ('  with Semaphore(profiling_slots=1):   ← 슬롯. 차 있으면 대기', 'mono'),
    ('      _running += 1 · fn(*args) · finally _running -= 1', 'mono'),
    ('stats() → {slots, running, submitted}   (/health)', 'mono'),
    ('', 'mono'),
    ('fn = run_and_callback  → 3단계로', 'sub'),
    ('기한(deadline)·취소는 아직 없음 (3단계 §12.1 240초는 다음)', 'sub'),
])
# 3 처리
x = XS[2]
lines(x + 12, TOP + 84, [
    ('h = input_hash(rq)   sha256(비선호 ID순 정렬한 rq)', 'mono'),
    ('0  store.save(ProfileOutcome(RUNNING, input_hash=h))', 'monob'),
    ('1  catalog_version_id, products = catalog.active()', 'monob'),
    ('2  needs_model(rq) → v1: False (취향 None·리뷰 [])', 'mono'),
    ('   validation = ValidationResult(disliked_tags=이름들)', 'mono'),
    ('3  search = build_pool(rq, products, pool_size, version)', 'monob'),
    ('   판매중 · 비선호(ID 또는 이름) 제외 · viewCount↓ id↑ · 30개', 'sub'),
    ('4  outcome = ProfileOutcome(RESULT_READY, …)', 'mono'),
    ('5  store.save(outcome)      ← 콜백 본문이 DB에 먼저', 'monob'),
    ('6  recipient_store.upsert(from_outcome(rq, outcome))', 'monob'),
    ('   types.py: from_outcome → cap_tags(12·8)', 'sub'),
    ('실패 → _fail(rq, store, reason) → FAILED, 콜백 없음', 'mono'),
    ('return outcome', 'monob'),
])
box(x + 12, TOP + 284, CW - 24, 52, 'v3', 3)
lines(x + 20, TOP + 300, [
    ('v3 자리(needs_model=True): 조인 → ProfileModel.analyze()', 'mono'),
    ('→ 검증기(관문 1~5) → Search.search() — Protocol만 있음', 'mono'),
    ('지금은 경고 로그 후 v1 경로로 처리', 'sub'),
])
# 4 콜백
x = XS[3]
lines(x + 12, TOP + 84, [
    ('if status != RESULT_READY: return  (FAILED = 침묵)', 'mono'),
    ('result = backend.send_profile_callback(outcome)', 'monob'),
    ('  to_callback(outcome) → callback_body()', 'mono'),
    ('    → ProfileCallbackRequest  snake → camel · 태그 없음', 'mono'),
    ('  httpx POST /api/internal/v1/recipients/{id}/profile', 'mono'),
    ('    Bearer SERVICE_TOKEN · timeout CALLBACK_TIMEOUT_S', 'sub'),
    ('  _status_from_response(code) → RunStatus', 'mono'),
    ('    200 DELIVERED · 409 SUPERSEDED · 4xx FAILED', 'sub'),
    ('    5xx·네트워크 RESULT_READY (미전달 = 재전송 대상)', 'sub'),
    ('  _error_code(res)  로그용 · 예외는 밖으로 안 냄', 'mono'),
    ('store.save(status=result, callback_attempts+1)', 'monob'),
    ('', 'mono'),
    ('재시도·백오프는 아직 없음 (#23)', 'sub'),
])

# 단계 사이 화살표 (자료형) — 상자 위 띠에
AY = TOP - 12
for i, label in enumerate(['run_and_callback + rq: ProfileRequest', 'fn(*args)', 'outcome: ProfileOutcome (RESULT_READY)']):
    x1, x2 = XS[i] + CW - 70, XS[i + 1] + 70
    path(f'M{x1} {AY} H{x2}')
    t((x1 + x2) / 2, AY - 6, label, 'sub', 'middle')

# ───────────────────── 포트 띠 ─────────────────────
PY = TOP + SH + 70
box(40, PY, W - 80, 40, 'band', 6)
t(52, PY + 16, 'ports.py — 업무가 바깥에 요구하는 모양 (typing.Protocol · @runtime_checkable). 업무 코드는 이 파일만 import한다', 'h2')
ports = ['CatalogReader  active() · by_id()', 'ProfileRunStore  save() · get()', 'RecipientProfileStore  upsert() · get() · delete()', 'BackendPort  send_profile_callback()']
for p, x in zip(ports, XS):
    t(x + CW / 2, PY + 33, p, 'port', 'middle')

# 단계 → 포트 (어느 단계가 어느 포트를 부르나). 직교 경로, 가로선은 y를 달리해 겹치지 않게
B = TOP + SH
path(f'M{XS[0]+60} {B} V{PY}'); t(XS[0] + 68, B + 26, '③ catalog.active()', 'sub')
path(f'M{XS[2]+140} {B} V{B+16} H{XS[1]+CW-110} V{PY}'); t(XS[2] + 146, B + 12, '0·5 store.save()', 'sub')
path(f'M{XS[3]+60} {B} V{B+30} H{XS[1]+CW-50} V{PY}'); t(XS[3] + 66, B + 26, 'store.save(콜백 결과)', 'sub')
path(f'M{XS[2]+40} {B} V{B+44} H{XS[0]+CW-60} V{PY}'); t(XS[2] + 46, B + 40, '1 catalog.active()', 'sub')
path(f'M{XS[2]+300} {B} V{PY}'); t(XS[2] + 308, B + 12, '6 recipient_store.upsert()', 'sub')
path(f'M{XS[3]+220} {B} V{PY}'); t(XS[3] + 228, B + 26, 'send_profile_callback()', 'sub')

# ───────────────────── 어댑터 ─────────────────────
AY0 = PY + 74
AH = 126
adapters = [
    ('FileCatalogReader', 'catalog.py', [
        '__init__(path): JSON 로드 → _product_dicts() 로 형식 판별',
        '  _is_raw_format · _adapt_raw_product → ProductRecord 검증',
        '  잘못된 행 제외 · productId 중복 제거',
        'active() → (FILE_CATALOG_VERSION_ID 고정 UUID, 상품 전체)',
        'by_id(product_id) → ProductRecord | None  (v3 리뷰 조인)']),
    ('DbProfileRunStore', 'stores.py', [
        'save(outcome): INSERT … ON CONFLICT (rid, sv) DO UPDATE',
        '  status·input_hash·attempt+1(RUNNING)·payload coalesce',
        '  callback_attempts greatest · error · callback_body()',
        'get(rid) → 최신 1행 → ProfileOutcome',
        'payload_hash() · delete_recipient()']),
    ('DbRecipientProfileStore', 'stores.py', [
        'upsert(profile): INSERT … ON CONFLICT (rid) DO UPDATE',
        '  WHERE 기존.source_version <= 새 버전 (낮으면 무시)',
        'get(rid) → RecipientProfile',
        'delete(rid) → bool',
        'v1 저장: source_version · disliked_categories · 태그']),
    ('HttpBackendPort', 'backend.py', [
        'send_profile_callback(outcome) → RunStatus',
        '  to_callback → callback_body (7.7 본문, 태그 없음)',
        '  _status_from_response · _error_code',
        'close() — lifespan 종료 시',
        '(STORE=memory: MemoryProfileRunStore — dict + Lock)']),
]
for (name, file, rows), x in zip(adapters, XS):
    box(x, AY0, CW, AH, 'box', 5)
    t(x + 12, AY0 + 20, name, 'h2'); t(x + 12 + len(name) * 8.2 + 10, AY0 + 20, '«구현»', 'sub')
    t(x + 12, AY0 + 36, file, 'sub')
    lines(x + 12, AY0 + 54, [(r, 'mono') for r in rows], 14)
    path(f'M{x+CW/2} {PY+40} V{AY0}', 'impl')          # 포트 → 구현

# ───────────────────── 바깥 ─────────────────────
EY = AY0 + AH + 38
EH = 76
ext = [
    ('파일  tests/fixtures/catalog_sample.json', ['111건 · 56카테고리 · viewCount(임의). 전체 4,231건은 .env로', '팀원 ai_search.catalog_* 테이블이 오면 교체'], 'ext'),
    ('PostgreSQL  ai_chat → ai_profile.profile_runs', ['실행 1건 = 1행 · (recipient_user_id, source_version) UNIQUE', 'status CHECK · callback_payload jsonb · alembic 0002'], 'db'),
    ('PostgreSQL  ai_chat → ai_profile.recipient_profiles', ['수신자 1명 = 1행 · recipient_user_id PK (시퀀스 없음)', 'preferred/disliked_tags · disliked_categories jsonb · 0001'], 'db'),
    ('Backend  (로컬: tools/fake_backend  :8081)', ['POST 7.7 수신 (실패 주입 ok/409/400/500/timeout)', 'GET 7.9 export · 시험 콘솔 /console'], 'ext'),
]
for (name, subs, cls_), x in zip(ext, XS):
    box(x, EY, CW, EH, cls_, 5)
    t(x + 12, EY + 22, name, 'h2')
    for i, sub in enumerate(subs):
        t(x + 12, EY + 42 + i * 15, sub, 'sub')
path(f'M{XS[0]+CW/2} {AY0+AH} V{EY}', 'grey'); t(XS[0] + CW / 2 + 8, AY0 + AH + 24, '읽기(시작 1회)', 'sub')
path(f'M{XS[1]+CW/2} {AY0+AH} V{EY}', 'write'); t(XS[1] + CW / 2 + 8, AY0 + AH + 24, 'UPSERT (0·5·콜백 뒤)', 'sub')
path(f'M{XS[2]+CW/2} {AY0+AH} V{EY}', 'write'); t(XS[2] + CW / 2 + 8, AY0 + AH + 24, 'UPSERT (6)', 'sub')
path(f'M{XS[3]+CW/2} {AY0+AH} V{EY}', 'grey'); t(XS[3] + CW / 2 + 8, AY0 + AH + 24, 'HTTP POST 7.7', 'sub')

# ───────────────────── 바닥: 자료형과 실패 경로 ─────────────────────
NY = EY + EH + 26
parts.append(f'<line x1="40" y1="{NY}" x2="{W-40}" y2="{NY}" class="rule"/>')
t(40, NY + 22, '자료형', 'h2')
t(110, NY + 22, 'ProfileExtractRequest(camel) → to_internal → ProfileRequest(snake) → ProfileOutcome(= profile_runs 행) → callback_body → ProfileCallbackRequest(camel) → RunStatus.   RecipientProfile(= recipient_profiles 행).   변환 함수는 to_internal · callback_body 둘뿐.', 'sub')
t(40, NY + 44, '실패 경로', 'h2')
t(110, NY + 44, '400/401/503은 접수에서 끝(백그라운드 없음) · 처리 중 예외는 전부 FAILED로 기록되고 콜백 없음(AI는 침묵, Backend가 PENDING 지속 시간으로 판정) · 콜백 5xx는 RESULT_READY로 남아 재전송 대상 · 백그라운드 예외는 run_and_callback이 잡아 로그.', 'sub')
t(40, NY + 66, '시험', 'h2')
t(110, NY + 66, '단위(tests/unit, STORE=memory): 가짜 CatalogReader·ProfileRunStore·RecipientProfileStore·BackendPort로 3·4단계, TestClient로 1~4 e2e   ·   통합(tests/integration): 진짜 PostgreSQL로 어댑터·앱 전체   ·   수동: fake_backend 콘솔 → 7.6 → 7.7 → psql', 'sub')
parts.append('</svg>')
(P / 'pipeline-map.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
