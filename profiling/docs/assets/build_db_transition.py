"""DB 전환 설명 그림 — 메모리 저장 → PostgreSQL 두 테이블(09-23). 요청 한 건이 DB에 무엇을 언제 쓰는지.
python3 build_db_transition.py → db-transition.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
2026-10-07: 설명문·열 주석·바닥 문단을 걷어내고 상자 이름 + 함수·열 이름만 남겼다. 설명은 docs/DB_전환_설명.md · DB_ERD.md 에 있다."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1600, 680
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="g" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#9aa4ad"/></marker>
<marker id="w" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#c0392b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.h{{font-size:16px;font-weight:700}}.h2{{font-size:14px;font-weight:700;fill:#3f4a54}}.sub{{font-size:12.5px;fill:#5e6873}}.msg{{font-size:13.5px}}
.mono{{font-family:Menlo,ui-monospace,monospace;font-size:12.5px;fill:#3f4a54}}.small{{font-size:13px;fill:#5e6873}}.num{{font-size:12px;font-weight:700;fill:#fff}}
.call{{fill:none;stroke:#3b6fb6;stroke-width:1.7;marker-end:url(#a)}}.grey{{fill:none;stroke:#9aa4ad;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#g)}}.write{{fill:none;stroke:#c0392b;stroke-width:2;marker-end:url(#w)}}
.box{{fill:#fff;stroke:#bcc5cd;stroke-width:1.3}}.new{{fill:#f5f8fd;stroke:#3b6fb6;stroke-width:1.6}}.old{{fill:#f7f8fa;stroke:#d5dbe0;stroke-width:1.2}}
.db{{fill:#fff7f2;stroke:#e0a080;stroke-width:1.4}}.tbl{{fill:#fff;stroke:#c0392b;stroke-width:1.4}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


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
t(40, 46, 'DB 전환 — 실행 기록·수신자 프로필을 PostgreSQL 두 테이블로 (v1, 2026-09-23)', 'title')
t(40, 70, '파랑 = 새로 생긴 코드 · 회색 = 그대로 · 빨강 번호 = DB 쓰기', 'small')

# ───────────── 위: 전 → 후 ─────────────
box(40, 88, W - 80, 64, 'old', 6)
for x, head, line_ in [(56, '실행 기록', 'dict → ai_profile.profile_runs  (DbProfileRunStore)'),
                       (580, '수신자 프로필', '없음 → ai_profile.recipient_profiles  (DbRecipientProfileStore)'),
                       (1120, '카탈로그', '파일 그대로  (FileCatalogReader)')]:
    t(x, 112, head, 'h2'); t(x, 136, line_, 'mono')

# ───────────── 아래: 요청 한 건의 흐름 ─────────────
TOP, LH = 180, 440
X0, W0 = 40, 270      # intake
X1, W1 = 330, 400     # pipeline
X2, W2 = 800, 320     # 구현
X3, W3 = 1160, 400    # DB
for x, w, name, cls_ in [(X0, W0, 'intake.py', 'box'), (X1, W1, 'pipeline.py  profile()', 'box'),
                         (X2, W2, '구현 + main.py', 'box'), (X3, W3, 'PostgreSQL  ai_chat', 'db')]:
    box(x, TOP, w, LH, cls_, 6)
    t(x + 12, TOP + 26, name, 'h')

# intake lane
bx, bw = X0 + 12, W0 - 24
box(bx, TOP + 50, bw, 46, 'box'); t(bx + 10, TOP + 70, '7.6 접수 → 202', 'h2'); t(bx + 10, TOP + 88, 'supervisor.submit()', 'mono')
box(bx, TOP + 120, bw, 36, 'new'); t(bx + 10, TOP + 143, 'profile(rq, …) → outcome', 'mono')
box(bx, TOP + 300, bw, 36, 'box'); t(bx + 10, TOP + 323, 'send_profile_callback(outcome)', 'mono')
box(bx, TOP + 370, bw, 36, 'new'); t(bx + 10, TOP + 393, 'store.save(마지막 상태)', 'mono')

# pipeline lane — 단계
px, pw = X1 + 12, W1 - 24
steps = [(50, 'new', '0  save(RUNNING, input_hash)'), (96, 'box', '1  catalog.active()'), (142, 'box', '2  validation (비선호 이름)'),
         (188, 'box', '3  build_pool() → 30개'), (234, 'new', '4·5  save(outcome RESULT_READY)'), (280, 'new', '6  recipient_store.upsert()'),
         (326, 'box', 'return outcome')]
for y, cls_, head in steps:
    box(px, TOP + y, pw, 32, cls_); t(px + 10, TOP + y + 21, head, 'mono')

# 구현 lane
ax, aw = X2 + 12, W2 - 24
impl = [(50, 'new', 'DbProfileRunStore', 'save() · get_run()'), (120, 'old', 'FileCatalogReader', 'active()'),
        (190, 'new', 'DbRecipientProfileStore', 'upsert()'), (300, 'old', 'HttpBackendPort', 'POST 7.7 → RunStatus'),
        (360, 'new', 'main.py lifespan', 'create_engine · 저장소 2개')]
for y, cls_, name, fn in impl:
    box(ax, TOP + y, aw, 46, cls_); t(ax + 10, TOP + y + 19, name, 'h2'); t(ax + 10, TOP + y + 37, fn, 'mono')

# DB lane — 두 테이블 (열 이름만)
tx, tw = X3 + 12, W3 - 24
def table(y, name, cols):
    hh = 34 + 17 * len(cols) + 8
    box(tx, TOP + y, tw, hh, 'tbl'); t(tx + 10, TOP + y + 21, name, 'h2')
    for i, c in enumerate(cols):
        t(tx + 12, TOP + y + 43 + i * 17, c, 'mono')
    return hh
table(50, 'ai_profile.profile_runs  (0002)', ['id PK', '(recipient_user_id, source_version) UNIQUE', 'status · attempt · input_hash',
                                              'callback_payload · callback_hash', 'callback_attempts · error', 'created_at · updated_at'])
table(250, 'ai_profile.recipient_profiles  (0001)', ['recipient_user_id PK · source_version', 'preferred_tags · disliked_tags',
                                                     'disliked_categories', 'created_at · updated_at'])

# ── 화살표 ──
R0, L1, R1, L2, R2, L3 = X0 + W0 - 12, X1 + 12, X1 + W1 - 12, X2 + 12, X2 + W2 - 12, X3 + 12
path(f'M{R0} {TOP+138} H{L1}')                                                       # intake → pipeline
path(f'M{R1} {TOP+66} H{L2}'); num(R1 + 22, TOP + 54, '1', RED)                      # 0 → DbProfileRunStore
path(f'M{R1} {TOP+112} H{R1+30} V{TOP+143} H{L2}', 'grey'); num(R1 + 22, TOP + 100, '2', GREY)   # 1 → FileCatalogReader
path(f'M{R1} {TOP+250} H{R1+58} V{TOP+80} H{L2}'); num(R1 + 22, TOP + 238, '3', RED)             # 4·5 → DbProfileRunStore
path(f'M{R1} {TOP+296} H{R1+30} V{TOP+213} H{L2}'); num(R1 + 22, TOP + 284, '4', RED)            # 6 → DbRecipientProfileStore
path(f'M{R0} {TOP+319} H{L2}', 'grey'); num(X1 - 16, TOP + 319, '5', GREY)                      # intake → HttpBackendPort (상자 사이 틈으로)
path(f'M{R0} {TOP+388} H{X1-10} V{TOP+36} H{ax+aw/2} V{TOP+48}'); num(X1 + 330, TOP + 24, '6', RED)  # 콜백 결과 저장
path(f'M{R2} {TOP+73} H{L3}', 'write'); t((R2 + L3) / 2, TOP + 65, '①③⑥', 'h2', 'middle')
path(f'M{R2} {TOP+213} H{R2+16} V{TOP+280} H{L3}', 'write'); t(R2 + 36, TOP + 272, '④', 'h2', 'middle')

parts.append('</svg>')
(P / 'db-transition.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
