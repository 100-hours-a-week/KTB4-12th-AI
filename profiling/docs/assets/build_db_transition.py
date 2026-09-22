"""DB 전환 설명 그림 — 메모리 저장 → PostgreSQL 두 테이블. 요청 한 건이 DB에 무엇을 언제 쓰는지.
python3 build_db_transition.py → db-transition.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)"""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1600, 1110
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:15px;font-weight:700}}.h2{{font-size:13.5px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12px;fill:#5e6873}}.msg{{font-size:13px}}.mono{{font-family:Menlo,ui-monospace,monospace;font-size:11px;fill:#3f4a54}}
.small{{font-size:13px;fill:#5e6873}}.num{{font-size:12px;font-weight:700;fill:#fff}}.col{{font-family:Menlo,ui-monospace,monospace;font-size:10.5px;fill:#3f4a54}}.colk{{font-family:Menlo,ui-monospace,monospace;font-size:10.5px;font-weight:700;fill:#18212b}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.7;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}.write{{fill:none;stroke:#c0392b;stroke-width:2;marker-end:url(#w)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.new{{fill:#f5f8fd;stroke:#3b6fb6;stroke-width:1.6}}.old{{fill:#f7f8fa;stroke:#d5dbe0;stroke-width:1.2}}.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.tbl{{fill:#fff;stroke:#c0392b;stroke-width:1.3}}
.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


def box(x, y, w, h, cls_='box', r=4):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls_}"/>')


def num(x, y, n, color='#3b6fb6'):
    parts.append(f'<circle cx="{x}" cy="{y}" r="10" fill="{color}"/><text x="{x}" y="{y+4}" text-anchor="middle" class="num">{n}</text>')


def path(d, cls_='call'):
    parts.append(f'<path d="{d}" class="{cls_}"/>')


RED, GREY = '#c0392b', '#9aa4ad'
t(40, 46, 'DB 전환 — 실행 기록·수신자 프로필을 메모리에서 PostgreSQL 두 테이블로 (v1)', 'title')
t(40, 72, '파랑 테두리 = 이번에 새로 생기거나 바뀐 코드 · 회색 = 그대로 · 빨강 = DB에 쓰는 순간. 번호는 요청 한 건 안의 순서(빨강 번호 = DB 쓰기). 카탈로그는 아직 파일(팀원 DB 전까지).', 'small')

# ───────────────────────── 위: 전환 전 → 후 요약 ─────────────────────────
y0 = 96
box(40, y0, W - 80, 118, 'old', 6)
t(56, y0 + 22, '무엇이 바뀌었나', 'h')
cols = [(70, '실행 기록 (ProfileRunStore)', 'MemoryProfileRunStore — dict[int, ProfileOutcome]', '재시작하면 사라짐 · 콜백 뒤 상태 갱신 없음',
         'DbProfileRunStore → ai_profile.profile_runs', '(수신자, 버전)당 1행 · RUNNING→RESULT_READY→DELIVERED · 콜백 본문 보관'),
        (590, '수신자 프로필 (RecipientProfileStore)', '없음 (목 파일만, pipeline 미연결)', 'Chat이 읽을 태그 저장소가 없었음',
         'DbRecipientProfileStore → ai_profile.recipient_profiles', '수신자당 1행 · 낮은 버전은 DB가 무시 · v1은 비선호 카테고리만'),
        (1130, '카탈로그 (CatalogReader)', 'FileCatalogReader — catalog_sample.json', '',
         '그대로 (파일)', '팀원 ai_search.catalog_* 테이블이 생기면 교체')]
for x, head, before, b2, after, a2 in cols:
    t(x, y0 + 46, head, 'h2')
    t(x, y0 + 66, '전: ' + before, 'mono')
    if b2:
        t(x + 24, y0 + 81, b2, 'sub')
    t(x, y0 + 99, '후: ' + after, 'mono')
    t(x + 24, y0 + 114, a2, 'sub')

# ───────────────────────── 아래: 요청 한 건의 흐름 + DB ─────────────────────────
TOP, LH = 240, 640
X0, W0 = 40, 270      # transport
X1, W1 = 330, 400     # pipeline
X2, W2 = 800, 320     # adapters
X3, W3 = 1160, 400    # DB
for x, w, name, sub, cls_ in [(X0, W0, 'intake.py', 'run_and_callback() — Supervisor 슬롯 안', 'box'),
                              (X1, W1, 'pipeline.py', 'profile(rq, catalog, store, recipient_store)', 'box'),
                              (X2, W2, '구현 + main.py', 'catalog·stores·backend 와 조립', 'box'),
                              (X3, W3, 'PostgreSQL  ai_chat', 'docker compose · alembic 0001·0002 · 스키마 ai_profile', 'db')]:
    box(x, TOP, w, LH, cls_, 6)
    t(x + 12, TOP + 24, name, 'h'); t(x + 12, TOP + 42, sub, 'sub')

# transport lane
bx, bw = X0 + 12, W0 - 24
box(bx, TOP + 70, bw, 56, 'box')
t(bx + 10, TOP + 89, '7.6 접수 → 202', 'h2'); t(bx + 10, TOP + 106, 'rq = to_internal(body)', 'mono'); t(bx + 10, TOP + 120, 'supervisor.submit(run_and_callback)', 'mono')
box(bx, TOP + 150, bw, 40, 'new')
t(bx + 10, TOP + 166, 'profile(rq, …, recipient_store=)', 'mono'); t(bx + 10, TOP + 182, '← outcome (RESULT_READY | FAILED)', 'sub')
box(bx, TOP + 480, bw, 40, 'box')
t(bx + 10, TOP + 496, 'send_profile_callback(outcome)', 'mono'); t(bx + 10, TOP + 512, '→ RunStatus (DELIVERED · SUPERSEDED · …)', 'sub')
box(bx, TOP + 560, bw, 40, 'new')
t(bx + 10, TOP + 576, 'store.save(outcome ← status=result,', 'mono'); t(bx + 10, TOP + 591, '                callback_attempts + 1)', 'mono')

# pipeline lane
px, pw = X1 + 12, W1 - 24
steps = [
    (70, 'new', '0  h = input_hash(rq)  ·  store.save(RUNNING, input_hash=h)', ['실패 → FAILED, 콜백 없음 (기록 없는 결과는 보내지 않는다)']),
    (130, 'box', '1  catalog.active()  → (version, products)', ['없으면 FAILED']),
    (190, 'box', '2  validation = 비선호 카테고리 이름만 disliked_tags 에', []),
    (230, 'box', '3  search = build_pool(비선호 제외 · 판매중 · 조회수순 30)', []),
    (270, 'new', '4·5  outcome = ProfileOutcome(RESULT_READY, search)', ['input_hash=h 포함 · store.save(outcome)   실패 → FAILED, 콜백 없음']),
    (330, 'new', '6  recipient_store.upsert(from_outcome(rq, outcome))', ['실패 → FAILED로 되돌림 (콜백은 나갔는데 Chat이 읽을 행이 없는 상태 방지)', 'recipient_store=None(메모리 모드)이면 건너뜀']),
    (410, 'box', 'return outcome   →  transport가 RESULT_READY면 콜백', []),
]
for y, cls_, head, subs in steps:
    hh = 24 + 15 * len(subs)
    box(px, TOP + y, pw, hh, cls_)
    t(px + 10, TOP + y + 17, head, 'mono')
    for i, s in enumerate(subs):
        t(px + 10, TOP + y + 32 + i * 15, s, 'sub')

# adapters lane
ax, aw = X2 + 12, W2 - 24
box(ax, TOP + 70, aw, 124, 'new')
t(ax + 10, TOP + 90, 'DbProfileRunStore', 'h2'); t(ax + 10, TOP + 106, 'stores.py  «ProfileRunStore 구현»', 'sub')
for i, s in enumerate(['save(outcome) INSERT…ON CONFLICT(rid,sv)', '  status·input_hash · attempt +1 (RUNNING)', '  callback_payload·hash = coalesce(새, 기존)',
                       '  callback_attempts = greatest · error', 'get(rid) → 최신 버전 1행 → ProfileOutcome']):
    t(ax + 10, TOP + 124 + i * 15, s, 'mono')
box(ax, TOP + 205, aw, 46, 'old'); t(ax + 10, TOP + 224, 'FileCatalogReader  (그대로)', 'h2'); t(ax + 10, TOP + 241, 'active() → (고정 UUID …0001, 111건)', 'mono')
box(ax, TOP + 330, aw, 102, 'new')
t(ax + 10, TOP + 350, 'DbRecipientProfileStore', 'h2'); t(ax + 10, TOP + 366, 'stores.py', 'sub'); t(ax + 10, TOP + 380, '«RecipientProfileStore 구현»', 'sub')
for i, s in enumerate(['upsert(profile)  ON CONFLICT (rid) DO UPDATE', '  WHERE 기존.source_version <= 새 버전', 'get(rid) · delete(rid)']):
    t(ax + 10, TOP + 398 + i * 15, s, 'mono')
box(ax, TOP + 480, aw, 46, 'old'); t(ax + 10, TOP + 499, 'HttpBackendPort  (그대로)', 'h2'); t(ax + 10, TOP + 516, 'POST 7.7 → 200/409/4xx/5xx → RunStatus', 'mono')
box(ax, TOP + 542, aw, 92, 'new')
t(ax + 10, TOP + 560, 'main.py  lifespan (조립)', 'h2')
for i, s in enumerate(['STORE=db → create_engine(DATABASE_URL)', '  연결·alembic 버전 확인 — 실패면 앱이 뜨지 않음', '  store = DbProfileRunStore(engine)', '  recipient_store = DbRecipientProfileStore(…)', 'STORE=memory → Memory… · recipient_store=None']):
    t(ax + 10, TOP + 576 + i * 13, s, 'mono')

# DB lane — 두 테이블
tx, tw = X3 + 12, W3 - 24
def table(y, name, sub, cols_):
    hh = 46 + 14 * len(cols_)
    box(tx, TOP + y, tw, hh, 'tbl')
    t(tx + 10, TOP + y + 18, name, 'h2'); t(tx + 10, TOP + y + 33, sub, 'sub')
    for i, (c, k) in enumerate(cols_):
        t(tx + 12, TOP + y + 50 + i * 14, c, 'colk' if k else 'col')
    return hh

table(70, 'ai_profile.profile_runs   (0002)', '실행 1건 = 1행 · 감사·재전송 기록 (작업 큐 아님)', [
    ('id uuid PK · (recipient_user_id, source_version) UNIQUE', True),
    ('input_hash          7.6 본문 sha256 — 같은 키·다른 입력 감지', False),
    ('status              RUNNING·RESULT_READY·DELIVERED·SUPERSEDED·FAILED', True),
    ('attempt             RUNNING 재진입마다 +1', False),
    ('catalog_version_id  uuid, 감사용 (FK 없음)', False),
    ('callback_payload    jsonb — 7.7 본문(recommendedProductIds)', True),
    ('callback_hash       payload sha256 · callback_attempts 시도 수', False),
    ('error               jsonb {"reason": …}  (FAILED)', False),
    ('created_at · updated_at', False),
    ('CHECK  결과 상태면 callback_payload·hash NOT NULL', True),
])
table(330, 'ai_profile.recipient_profiles   (0001)', '수신자 1명 = 1행 · Chat이 태그를 읽는 곳 (DR-035)', [
    ('recipient_user_id   bigint PK — Backend ID 그대로 (시퀀스 없음)', True),
    ('source_version      낮은 버전으로 덮어쓰지 않음', True),
    ('preferred_tags      jsonb  v1: []', False),
    ('disliked_tags       jsonb  v1: 비선호 카테고리 이름', False),
    ('disliked_categories jsonb  [{category_id, category_name}]', True),
    ('created_at · updated_at', False),
    ('(v3 0003) profile_run_id FK · axes · recommended_ids …', False),
])

# ── 화살표 ──
R0, L1, R1, L2, R2, L3 = X0 + W0 - 12, X1 + 12, X1 + W1 - 12, X2 + 12, X2 + W2 - 12, X3 + 12
# transport → pipeline
path(f'M{R0} {TOP+170} H{L1}')
# ① step0 → DbProfileRunStore
path(f'M{R1} {TOP+90} H{L2}'); num(R1 + 20, TOP + 78, '1', RED)
# ② step1 → FileCatalogReader (회색, 직교)
path(f'M{R1} {TOP+150} H{R1+28} V{TOP+228} H{L2}', 'grey'); num(R1 + 20, TOP + 138, '2', GREY)
# ③ step4·5 → DbProfileRunStore
path(f'M{R1} {TOP+290} H{R1+56} V{TOP+170} H{L2}'); num(R1 + 20, TOP + 278, '3', RED)
# ④ step6 → DbRecipientProfileStore
path(f'M{R1} {TOP+350} H{L2}'); num(R1 + 20, TOP + 338, '4', RED)
# ⑤ transport → HttpBackendPort (pipeline 통과, 회색)
path(f'M{R0} {TOP+500} H{L2}', 'grey'); num(X1 + 200, TOP + 488, '5', GREY); t(X1 + 216, TOP + 492, '7.7 POST', 'sub')
# ⑥ transport store.save(DELIVERED) → DbProfileRunStore (위로 돌아서)
path(f'M{R0} {TOP+580} H{X1-10} V{TOP+58} H{ax+aw/2} V{TOP+70}'); num(X1 + 322, TOP + 46, '6', RED); t(X1 + 338, TOP + 50, '콜백 결과 저장', 'sub')
# DB 쓰기 (빨강)
path(f'M{R2} {TOP+130} H{L3}', 'write'); t((R2 + L3) / 2, TOP + 122, '①③⑥', 'h2', 'middle')
path(f'M{R2} {TOP+386} H{L3}', 'write'); t((R2 + L3) / 2, TOP + 378, '④', 'h2', 'middle')

# ── 아래 노트 ──
yb = TOP + LH + 18
parts.append(f'<line x1="40" y1="{yb}" x2="{W-40}" y2="{yb}" class="rule"/>')
t(40, yb + 24, 'DB에 쓰는 순간', 'h2')
t(150, yb + 24, '①  INSERT profile_runs (RUNNING, input_hash)      ③  UPDATE RESULT_READY + callback_payload·hash·catalog_version_id  — 콜백보다 먼저 커밋      '
                '④  UPSERT recipient_profiles (WHERE 버전 가드)      ⑥  UPDATE status(콜백 결과)·callback_attempts+1', 'sub')
t(40, yb + 50, '실패했을 때', 'h2')
t(150, yb + 50, '①·③ 저장 실패 → FAILED, 콜백 없음(기록 없는 결과를 보내지 않는다)      ④ 실패 → 실행 기록을 FAILED로 되돌림      ⑤ 5xx·네트워크 → RESULT_READY 유지 = 재전송 대상(재시도 자체는 #23)      '
                'AI는 실패를 알리지 않고 Backend가 판정', 'sub')
t(40, yb + 78, '확인', 'h2')
t(150, yb + 78, 'GET /health → "store": {"backend": "db", "connected": true, "migration": "0002"}     ·     STORE=memory 로 띄우면 이전 동작(메모리 · 프로필 저장 없음) — 단위 테스트가 이 모드', 'mono')
t(150, yb + 96, 'docker compose exec ai-db psql -U ai_user -d ai_chat -c "select recipient_user_id, source_version, status, attempt, callback_attempts, updated_at from ai_profile.profile_runs order by updated_at desc limit 5"', 'mono')
parts.append('</svg>')
(P / 'db-transition.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
