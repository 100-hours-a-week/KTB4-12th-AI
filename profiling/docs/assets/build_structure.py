"""구조 그림 — src/profiling/ 모듈이 어느 층에 있고 의존이 어느 방향인지. README §1의 그림.
python3 build_structure.py → structure.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
2026-10-07: 설명문을 걷어내고 상자 이름 + 함수 이름만 남겼다. 각 모듈이 하는 일은 README §1 표와 docs/코드_안내서.md 에 있다.
모듈이 늘거나 포트가 바뀌면 여기도 고친다."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1700, 790
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:16px;font-weight:700}}.h2{{font-size:14px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12.5px;fill:#5e6873}}.msg{{font-size:13.5px}}
.mono{{font-family:Menlo,ui-monospace,monospace;font-size:12.5px;fill:#3f4a54}}.monob{{font-family:Menlo,ui-monospace,monospace;font-size:15px;font-weight:700;fill:#18212b}}
.small{{font-size:13px;fill:#5e6873}}.tag{{font-size:12px;font-weight:700;fill:#8a94a0;letter-spacing:.4px}}
.port{{font-family:Menlo,ui-monospace,monospace;font-size:14px;font-weight:700;fill:#3b6fb6}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.8;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}
.write{{fill:none;stroke:#c0392b;stroke-width:1.8;marker-end:url(#w)}}.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.3;stroke-dasharray:6 4;marker-end:url(#a)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.stage{{fill:#fff;stroke:#3b6fb6;stroke-width:1.6}}.band{{fill:#eef3fa;stroke:#9db0d2;stroke-width:1.2}}
.core{{fill:#f4f8ff;stroke:#3b6fb6;stroke-width:1.8}}.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.ext{{fill:#f7f8fa;stroke:#bcc5cd;stroke-width:1.2}}
</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


def box(x, y, w, h, cls_='box', r=5):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="{cls_}"/>')


def path(d, cls_='call'):
    parts.append(f'<path d="{d}" class="{cls_}"/>')


# ---------------------------------------------------------------- 머리
t(40, 46, 'profiling 구조 — 모듈과 의존 방향 (v1)', 'title')
t(40, 70, '파랑 호출 · 빨강 DB 쓰기 · 회색 점선 조립 · 화살표는 안쪽(업무)으로', 'small')

LANES = [(170, 350), (546, 350), (922, 350), (1298, 350)]   # (x, w) — 포트 4개가 세로로 한 줄씩

# ---------------------------------------------------------------- 조립 (main.py)
box(40, 90, W - 80, 46, 'band')
t(56, 119, 'main.py', 'monob')
t(150, 119, 'lifespan() — 구현 4개 + Supervisor 를 만들어 app.state 에 둔다 · GET /health', 'msg')
t(W - 56, 119, 'settings.py — PROFILING_*', 'sub', 'end')
for _x, _w in LANES:                                   # 조립 점선 — 상자 뒤에 깔린다
    path(f'M {_x+_w/2} 136 L {_x+_w/2} 520', 'grey')

# ---------------------------------------------------------------- 경계
TOP = 168
t(40, TOP - 8, '경계', 'tag')
box(40, TOP, 300, 74, 'ext')
t(56, TOP + 26, 'Backend', 'h')
t(56, TOP + 50, 'POST 7.6  extract-and-pool', 'mono')

box(390, TOP, 420, 74, 'stage')
t(406, TOP + 26, 'intake.py', 'monob')
t(406, TOP + 48, 'extract_and_pool() → 202 PENDING', 'mono')
t(406, TOP + 65, 'dispatch() → run_and_callback | resend_callback', 'mono')

box(840, TOP, 360, 74, 'stage')
t(856, TOP + 26, 'schemas.py', 'monob')
t(856, TOP + 50, '7.6 · 7.7 · 7.9 DTO  (camelCase)', 'mono')

box(1230, TOP, 430, 74, 'stage')
t(1246, TOP + 26, 'supervisor.py', 'monob')
t(1246, TOP + 50, 'submit() → 큐 → 워커 N  (가득이면 503)', 'mono')

path(f'M 340 {TOP+37} L 384 {TOP+37}')
path(f'M 810 {TOP+58} L 836 {TOP+58}', 'grey')
path(f'M 1200 {TOP+37} L 1224 {TOP+37}')

# ---------------------------------------------------------------- 업무 (안쪽)
MID = 288
t(40, MID - 8, '업무 — 바깥을 모른다', 'tag')
box(40, MID, W - 80, 96, 'core')
box(66, MID + 16, 780, 64, 'box')
t(82, MID + 40, 'pipeline.py', 'monob')
t(82, MID + 62, 'profile()  ·  to_internal  ·  needs_model  ·  build_pool', 'mono')
box(874, MID + 16, 760, 64, 'box')
t(890, MID + 40, 'types.py', 'monob')
t(890, MID + 62, 'ProfileRequest · ProfileOutcome · RecipientProfile · RunStatus', 'mono')

path(f'M 590 {TOP+74} L 590 {MID-2}')
t(600, MID - 16, 'run_and_callback()', 'mono')

# ---------------------------------------------------------------- 포트
PORT_Y = 428
t(40, PORT_Y - 10, '포트 (typing.Protocol)', 'tag')
box(40, PORT_Y, W - 80, 50, 'band')
t(56, PORT_Y + 31, 'ports.py', 'monob')
for (x, w), name in zip(LANES, ['CatalogReader', 'ProfileRunStore', 'RecipientProfileStore', 'BackendPort']):
    t(x + w / 2, PORT_Y + 31, name, 'port', 'middle')
path(f'M 1560 {MID+96} L 1560 {PORT_Y-2}')

# ---------------------------------------------------------------- 구현
IMPL_Y = 528
t(40, IMPL_Y - 10, '구현', 'tag')
IMPL = [('catalog.py', 'DbCatalogReader / FileCatalogReader', 'active()'),
        ('stores.py', 'DbProfileRunStore', 'save() · get_run() · run_lock()'),
        ('stores.py', 'DbRecipientProfileStore', 'upsert() · get()'),
        ('backend.py', 'HttpBackendPort', 'send_profile_callback()')]
for (x, w), (mod, cls_, fn) in zip(LANES, IMPL):
    box(x, IMPL_Y, w, 70, 'box')
    t(x + 16, IMPL_Y + 28, cls_, 'h2')
    t(x + w - 16, IMPL_Y + 28, mod, 'mono', 'end')
    t(x + 16, IMPL_Y + 52, fn, 'mono')
    path(f'M {x+w/2} {IMPL_Y-2} L {x+w/2} {PORT_Y+52}', 'impl')

# ---------------------------------------------------------------- 바깥
OUT_Y = 660
t(40, OUT_Y - 10, '바깥', 'tag')
OUT = [('PostgreSQL  ai_catalog', 'db', '상품 4,231 · 카테고리 67'),
       ('PostgreSQL  ai_profile.profile_runs', 'db', '실행 1건 = 1행'),
       ('PostgreSQL  ai_profile.recipient_profiles', 'db', '수신자 1명 = 1행'),
       ('Backend', 'ext', 'POST 7.7  상품 ID ≤30')]
for (x, w), (name, cls_, sub) in zip(LANES, OUT):
    box(x, OUT_Y, w, 64, cls_)
    t(x + 16, OUT_Y + 27, name, 'h2')
    t(x + 16, OUT_Y + 50, sub, 'mono')
    path(f'M {x+w/2} {IMPL_Y+70} L {x+w/2} {OUT_Y-2}', 'write' if cls_ == 'db' else 'call')

parts.append('</svg>')
(P / 'structure.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
