"""파이프라인 지도 — 요청 한 건이 지나는 4단계와, 포트를 거쳐 연결되는 구현·바깥. README §1 과 docs/파이프라인_지도/ 의 그림.
python3 build_pipeline_map.py → pipeline-map.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
2026-10-07: 설명문을 걷어내고 단계 이름 + 함수 이름만 남겼다. 단계마다 부르는 함수의 자세한 목록은 그날 지도 문서의 §1 표에 있다.
코드가 바뀌면 여기도 고친다 (기준 2026-10-06, BE 콜백 PR #247 실물 연결 뒤)."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1700, 760
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:17px;font-weight:700}}.h2{{font-size:14px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12.5px;fill:#5e6873}}.msg{{font-size:13.5px}}
.mono{{font-family:Menlo,ui-monospace,monospace;font-size:12.5px;fill:#3f4a54}}.monob{{font-family:Menlo,ui-monospace,monospace;font-size:13px;font-weight:700;fill:#18212b}}
.small{{font-size:13px;fill:#5e6873}}.port{{font-family:Menlo,ui-monospace,monospace;font-size:14px;font-weight:700;fill:#3b6fb6}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.8;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}.write{{fill:none;stroke:#c0392b;stroke-width:1.8;marker-end:url(#w)}}
.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.3;stroke-dasharray:6 4;marker-end:url(#a)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.stage{{fill:#fff;stroke:#3b6fb6;stroke-width:1.6}}.band{{fill:#eef3fa;stroke:#9db0d2;stroke-width:1.2}}
.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.ext{{fill:#f7f8fa;stroke:#bcc5cd;stroke-width:1.2}}.v3{{fill:#fffbe6;stroke:#e6d98a;stroke-width:1;stroke-dasharray:4 3}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


def box(x, y, w, h, cls_='box', r=4):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls_}"/>')


def path(d, cls_='call'):
    parts.append(f'<path d="{d}" class="{cls_}"/>')


def lines(x, y, rows, dy=18):
    for i, s in enumerate(rows):
        t(x, y + i * dy, s, 'mono')


t(40, 44, '파이프라인 지도 — 요청 한 건의 4단계와 연결 (v1, 2026-10-06 코드)', 'title')
t(40, 68, '파랑 호출 · 빨강 DB 쓰기 · 회색 점선 HTTP · 노랑 점선 v3 자리(미구현)', 'small')

# ───────────────────── 조립 ─────────────────────
box(40, 86, W - 80, 44, 'band')
t(56, 114, 'main.py lifespan()', 'monob')
t(230, 114, '카탈로그 리더 · 저장소 2 · Backend 포트 · Supervisor → app.state   ·   GET /health', 'msg')

# ───────────────────── 4단계 ─────────────────────
XS = [40, 455, 870, 1285]
CW = 375
TOP = 180
SH = 150
STAGES = [
    ('1 접수', 'intake.py', ['POST 7.6  extract-and-pool', '검증 400 · 토큰 401 · 카탈로그 없음 503', 'supervisor.submit()  가득이면 503', '→ 202 PENDING']),
    ('2 큐 · 워커', 'supervisor.py', ['submit() → queue → _worker()', 'dispatch(): run_lock() → decide()', 'analyze · resend · skip']),
    ('3 처리', 'pipeline.py', ['profile()', 'save(RUNNING) → catalog.active()', 'build_pool()  재고·비선호 제외 · 조회수↓ 30', 'save(결과) → recipient_store.upsert()']),
    ('4 콜백 · 기록', 'intake.py · backend.py', ['send_profile_callback()', 'POST 7.7 → 200 DELIVERED · 409 SUPERSEDED', '4xx FAILED · 5xx 즉시 3회', 'store.save(마지막 상태)']),
]
for x, (name, mod, rows) in zip(XS, STAGES):
    box(x, TOP, CW, SH, 'stage')
    t(x + 14, TOP + 28, name, 'h')
    t(x + CW - 14, TOP + 28, mod, 'sub', 'end')
    lines(x + 14, TOP + 58, rows)
# v3 자리 (3 처리 상자 안)
box(XS[2] + 14, TOP + SH - 30, CW - 28, 22, 'v3', 3)
t(XS[2] + 22, TOP + SH - 14, 'v3 자리 — ProfileModel · Search (미구현)', 'sub')

# 단계 사이 화살표
for i, label in enumerate(['rq', 'fn', 'outcome']):
    x1, x2 = XS[i] + CW, XS[i + 1]
    path(f'M{x1} {TOP+40} H{x2-2}')
    t((x1 + x2) / 2, TOP - 10, label, 'sub', 'middle')

# ───────────────────── 포트 띠 ─────────────────────
PY = 388
box(40, PY, W - 80, 46, 'band')
t(56, PY + 29, 'ports.py', 'monob')
PORTS = ['CatalogReader', 'ProfileRunStore', 'RecipientProfileStore', 'BackendPort']
for x, name in zip(XS, PORTS):
    t(x + CW / 2, PY + 29, name, 'port', 'middle')
for x in XS:                                           # 단계 → 포트
    path(f'M{x+CW/2} {TOP+SH} V{PY-2}')

# ───────────────────── 구현 ─────────────────────
AY = 480
ADAPTERS = [('DbCatalogReader / FileCatalogReader', 'catalog.py', 'active()'),
            ('DbProfileRunStore', 'stores.py', 'save() · get_run() · run_lock()'),
            ('DbRecipientProfileStore', 'stores.py', 'upsert() · get()'),
            ('HttpBackendPort', 'backend.py', 'send_profile_callback()')]
for x, (name, mod, fn) in zip(XS, ADAPTERS):
    box(x, AY, CW, 66, 'box')
    t(x + 14, AY + 27, name, 'h2')
    t(x + CW - 14, AY + 27, mod, 'sub', 'end')
    t(x + 14, AY + 50, fn, 'mono')
    path(f'M{x+CW/2} {AY-2} V{PY+48}', 'impl')

# ───────────────────── 바깥 ─────────────────────
EY = 600
OUTER = [('PostgreSQL  ai_catalog', 'db', '상품 4,231 · 활성 버전 1개', 'write'),
         ('PostgreSQL  ai_profile.profile_runs', 'db', '(수신자, 번호) UNIQUE · 콜백 본문 보관', 'write'),
         ('PostgreSQL  ai_profile.recipient_profiles', 'db', '수신자 1명 = 1행 · 태그 보관', 'write'),
         ('Backend  develop :8080 · 가짜 :8081', 'ext', '7.7 수신 — 200 · 409 · 400', 'grey')]
for x, (name, cls_, sub, arrow) in zip(XS, OUTER):
    box(x, EY, CW, 64, cls_)
    t(x + 14, EY + 27, name, 'h2')
    t(x + 14, EY + 50, sub, 'mono')
    path(f'M{x+CW/2} {AY+66} V{EY-2}', arrow)

parts.append('</svg>')
(P / 'pipeline-map.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
