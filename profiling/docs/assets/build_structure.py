"""구조 그림 — src/profiling/ 모듈 12개가 어느 자리에 있고 의존이 어느 방향으로 흐르는지. README §1의 그림.
python3 build_structure.py → structure.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
모듈이 늘거나 포트가 바뀌면 여기도 고친다 (코드 기준 2026-09-23, 패키지 평탄화 + 저장소 DB 전용화 후)."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1700, 1015
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:15px;font-weight:700}}.h2{{font-size:13px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12px;fill:#5e6873}}.msg{{font-size:13px}}
.mono{{font-family:Menlo,ui-monospace,monospace;font-size:11px;fill:#3f4a54}}.monob{{font-family:Menlo,ui-monospace,monospace;font-size:13px;font-weight:700;fill:#18212b}}
.small{{font-size:13px;fill:#5e6873}}.tag{{font-size:11px;font-weight:700;fill:#8a94a0;letter-spacing:.4px}}
.port{{font-family:Menlo,ui-monospace,monospace;font-size:13px;font-weight:700;fill:#3b6fb6}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.8;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}
.write{{fill:none;stroke:#c0392b;stroke-width:1.8;marker-end:url(#w)}}.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.3;stroke-dasharray:6 4;marker-end:url(#a)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.stage{{fill:#fff;stroke:#3b6fb6;stroke-width:1.6}}.band{{fill:#eef3fa;stroke:#9db0d2;stroke-width:1.2}}
.core{{fill:#f4f8ff;stroke:#3b6fb6;stroke-width:1.8}}.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.ext{{fill:#f7f8fa;stroke:#bcc5cd;stroke-width:1.2}}
.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


def box(x, y, w, h, cls_='box', r=5):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls_}"/>')


def path(d, cls_='call'):
    parts.append(f'<path d="{d}" class="{cls_}"/>')


def lines(x, y, rows, dy=15):
    for i, (s, c) in enumerate(rows):
        t(x, y + i * dy, s, c)


# ---------------------------------------------------------------- 머리
t(40, 46, 'profiling 구조 — src/profiling/ 모듈 12개와 의존 방향 (v1, 2026-09-23)', 'title')
t(40, 70, 'Ports & Adapters(헥사고날). 업무 코드는 바깥(HTTP·파일·DB)을 모른다. 바깥이 업무가 정한 "포트(모양)"에 맞춰 들어온다 — 화살표는 항상 안쪽을 향한다.', 'small')

LANES = [(170, 350), (546, 350), (922, 350), (1298, 350)]   # (x, w) — 포트 4개가 세로로 한 줄씩

# ---------------------------------------------------------------- 조립 (main.py)
box(40, 92, W - 80, 62, 'band')
t(56, 114, 'main.py', 'monob')
t(140, 114, '[조립 · Composition Root]  여기서만 구체 클래스를 안다 — lifespan에서 4개를 만들어 app.state에 두고, 종료 때 정리한다 (아래 회색 점선).', 'msg')
t(56, 136, 'FileCatalogReader(CATALOG_FILE) · _connect_db(DATABASE_URL) → DbProfileRunStore(engine) · DbRecipientProfileStore(engine) · HttpBackendPort(BACKEND_BASE_URL) · Supervisor(PROFILING_SLOTS)', 'mono')
t(W - 56, 114, 'settings.py — 환경변수 PROFILING_* 한 곳', 'sub', 'end')
t(W - 56, 136, '__init__.py — 공개 표면(profile · 자료형 · Settings)', 'sub', 'end')

# 조립 점선 — 먼저 그려 두면 아래에서 그리는 상자들이 덮어, 띠 사이 틈에서만 보인다
for _x, _w in LANES:
    path(f'M {_x+_w/2} 156 L {_x+_w/2} 588', 'grey')

# ---------------------------------------------------------------- 경계 (Transport)
TOP = 186
t(40, TOP - 8, '경계 — 바깥 계약이 닿는 곳', 'tag')
box(40, TOP, 300, 96, 'ext')
t(56, TOP + 24, 'Backend', 'h')
lines(56, TOP + 44, [('POST 7.6 extract-and-pool', 'mono'), ('Authorization: Bearer <토큰>', 'mono'), ('카탈로그 없으면 503 · 검증 실패 400', 'sub')])

box(390, TOP, 420, 96, 'stage')
t(406, TOP + 24, 'intake.py', 'monob')
lines(406, TOP + 44, [('[Transport] 토큰 → 검증 → 202 PENDING', 'msg'),
                      ('extract_and_pool() · run_and_callback()', 'mono'),
                      ('여기서 Backend와의 HTTP가 끝난다', 'sub')])

box(840, TOP, 360, 96, 'stage')
t(856, TOP + 24, 'schemas.py', 'monob')
lines(856, TOP + 44, [('[바깥 계약] 7.6·7.7·7.9 DTO', 'msg'),
                      ('camelCase — 문서 1과 1:1', 'mono'),
                      ('모르는 필드는 무시하고 경고만', 'sub')])

box(1230, TOP, 430, 96, 'stage')
t(1246, TOP + 24, 'supervisor.py', 'monob')
lines(1246, TOP + 44, [('[Supervisor] 프로파일링 슬롯(동시 1)', 'msg'),
                       ('submit() → 응답 뒤 백그라운드에서 실행', 'mono'),
                       ('기한·취소·재시작 복구는 다음', 'sub')])

path(f'M 340 {TOP+40} L 384 {TOP+40}')
path(f'M 810 {TOP+70} L 836 {TOP+70}', 'grey')
path(f'M 1200 {TOP+40} L 1224 {TOP+40}')

# ---------------------------------------------------------------- 업무 (안쪽)
MID = 322
t(40, MID - 8, '업무 (안쪽) — 바깥을 모른다. 인자로 받은 포트 모양만 쓴다', 'tag')
box(40, MID, W - 80, 118, 'core')
box(66, MID + 20, 780, 78, 'box')
t(82, MID + 44, 'pipeline.py', 'monob')
lines(82, MID + 64, [('profile() — 0 RUNNING 기록 → 1 카탈로그 → 2 검증 → 3 풀 30개 → 4 결과 → 5 기록 → 6 프로필', 'mono'),
                     ('to_internal · input_hash · needs_model · build_pool.  예외는 FAILED로 바꿔 돌려주고 밖으로 안 던진다', 'mono')])
box(874, MID + 20, 760, 78, 'box')
t(890, MID + 44, 'types.py', 'monob')
lines(890, MID + 64, [('[내부 자료형 snake_case] ProfileRequest · ValidationResult · SearchResult', 'mono'),
                      ('ProfileOutcome(=profile_runs 행) · RecipientProfile(=recipient_profiles 행) · RunStatus · ErrorCode', 'mono')])

path(f'M 590 {TOP+96} L 590 {MID-2}')
t(600, MID - 14, 'run_and_callback(rq, catalog, store, backend, recipient_store)', 'mono')

# ---------------------------------------------------------------- 포트
PORT_Y = 484
t(40, PORT_Y - 10, '포트 — 업무가 바깥에 요구하는 "모양" (typing.Protocol · ports.py). 업무 코드는 이 파일만 import한다', 'tag')
box(40, PORT_Y, W - 80, 52, 'band')
t(56, PORT_Y + 31, 'ports.py', 'monob')
PORTS = ['CatalogReader', 'ProfileRunStore', 'RecipientProfileStore', 'BackendPort']
for (x, w), name in zip(LANES, PORTS):
    t(x + w / 2, PORT_Y + 31, name, 'port', 'middle')

path(f'M 1560 {MID+118} L 1560 {PORT_Y-2}')
t(1548, PORT_Y - 10, '포트 모양으로만 부른다', 'sub', 'end')

# ---------------------------------------------------------------- 구현
IMPL_Y = 590
t(40, IMPL_Y - 10, '구현 — 포트에 꽂히는 실제 코드. 업무 코드는 이 파일들을 import하지 않는다', 'tag')
IMPL = [('catalog.py', 'FileCatalogReader', ['active() → (버전, 상품 전체)', 'by_id(product_id)', '7.9 형식·원형 자동 판별·검증']),
        ('stores.py', 'DbProfileRunStore', ['save() — UPSERT 한 문장', 'get() — 최신 1행', 'payload_hash · delete_recipient']),
        ('stores.py', 'DbRecipientProfileStore', ['upsert() — 버전 가드', 'get() · delete()', '낮은 버전은 DB가 무시']),
        ('backend.py', 'HttpBackendPort', ['send_profile_callback()', '→ CallbackResult(상태·코드)', 'to_callback · callback_body'])]
for (x, w), (mod, cls_, rows) in zip(LANES, IMPL):
    box(x, IMPL_Y, w, 118, 'box')
    t(x + 16, IMPL_Y + 26, cls_, 'h')
    t(x + w - 16, IMPL_Y + 26, mod, 'mono', 'end')
    lines(x + 16, IMPL_Y + 48, [(r, 'mono') for r in rows])
    path(f'M {x+w/2} {IMPL_Y-2} L {x+w/2} {PORT_Y+54}', 'impl')
t(LANES[3][0] + LANES[3][1] / 2 + 12, PORT_Y + 78, '«구현»', 'sub')

# ---------------------------------------------------------------- 바깥
OUT_Y = 760
t(40, OUT_Y - 10, '바깥 — 실제 자원', 'tag')
OUT = [('카탈로그 JSON 파일', 'ext', ['PROFILING_CATALOG_FILE', '예시 111건 · 전체 4,231건', 'BE 7.9 export 형식']),
       ('PostgreSQL  ai_profile.profile_runs', 'db', ['실행 1건 = 1행', '(수신자, source_version) UNIQUE', 'alembic 0002']),
       ('PostgreSQL  ai_profile.recipient_profiles', 'db', ['수신자 1명 = 1행', '태그는 여기 보관 (DR-035)', 'alembic 0001']),
       ('Backend', 'ext', ['POST 7.7 상품 ID ≤30', '200 DELIVERED · 409 SUPERSEDED', '4xx FAILED · 5xx 재전송 대상'])]
for (x, w), (name, cls_, rows) in zip(LANES, OUT):
    box(x, OUT_Y, w, 96, cls_)
    t(x + 16, OUT_Y + 26, name, 'h2')
    lines(x + 16, OUT_Y + 46, [(r, 'mono') for r in rows])
    path(f'M {x+w/2} {IMPL_Y+118} L {x+w/2} {OUT_Y-2}', 'write' if cls_ == 'db' else 'call')

# ---------------------------------------------------------------- 규칙
NY = 890
parts.append(f'<line x1="40" y1="{NY}" x2="{W-40}" y2="{NY}" class="rule"/>')
lines(40, NY + 26, [
    ('이름 규칙 — HTTP 경계만 camelCase(schemas.py). 그 밖의 파이썬은 전부 snake_case. 두 세계를 잇는 변환은 pipeline.to_internal() 과 backend.to_callback() 두 함수뿐이다.', 'small'),
    ('바꿔 끼우기 — 포트가 같은 모양이면 무엇이든 꽂힌다. 단위 테스트는 가짜 구현을, 운영은 위 4개를 쓴다. 파일 카탈로그를 DB로 바꿔도 pipeline.py 는 한 줄도 바뀌지 않는다.', 'small'),
    ('아직 없는 것 — 팀원 Search 호출(v3, 질의어가 생길 때) · ProfileModel(LLM 태그 추출, v3) · 카탈로그 DB adapter. 그때 포트가 하나씩 늘고 구현 칸이 채워진다.', 'small'),
], 22)

parts.append('</svg>')
(P / 'structure.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
