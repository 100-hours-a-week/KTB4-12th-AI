"""v1 프로파일링 한 건 — 파일 사이의 호출 순서를 시퀀스로. python3 build_v1_flow.py → v1-flow.svg (PNG는 Chrome 헤드리스)
2026-10-07: 노란 설명 상자와 바닥 문단을 걷어내고 호출 이름만 남겼다. 각 단계의 뜻은 README §1 "요청 한 건의 흐름" 1~3 에 있다."""
from pathlib import Path
from xml.sax.saxutils import escape
P = Path(__file__).resolve().parent; W, H = 1460, 760
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="r" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#77818b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}.head{{font-size:16px;font-weight:700}}.sub{{font-size:12.5px;fill:#5e6873}}.msg{{font-size:14px;fill:#18212b}}.mono{{font-family:Menlo,ui-monospace,monospace;font-size:13px;fill:#3f4a54}}.small{{font-size:13px;fill:#5e6873}}.num{{font-size:12px;font-weight:700;fill:#fff}}
.life{{stroke:#c9d0d6;stroke-width:1.2;stroke-dasharray:4 4}}.call{{fill:none;stroke:#3b6fb6;stroke-width:1.8;marker-end:url(#a)}}.ret{{fill:none;stroke:#77818b;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#r)}}.act{{fill:#dfe8f5;stroke:#9db0d2;stroke-width:1}}.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='msg', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


L = [('Backend', '7.6 보냄 · 7.7 받음'), ('intake.py', '접수 · 콜백'), ('pipeline.py', '업무'), ('catalog.py', '카탈로그'),
     ('stores.py', '실행 기록'), ('backend.py', '7.7 전송'), ('Backend 7.7', 'develop · 가짜')]
xs = [110, 350, 600, 830, 1020, 1210, 1370]
t(40, 46, 'v1 프로파일링 한 건 — 호출 순서', 'title')
t(40, 70, '실선 파랑 호출 · 점선 회색 반환 · 숫자는 순서', 'small')
TOP = 96; BOT = 715
for (n, sub), x in zip(L, xs):
    w = 170 if x not in (110, 1370) else 150
    parts.append(f'<rect x="{x-w/2}" y="{TOP}" width="{w}" height="54" rx="4" fill="#fff" stroke="#bcc5cd" stroke-width="1.3"/>')
    t(x, TOP + 23, n, 'head', 'middle')
    t(x, TOP + 42, sub, 'sub', 'middle')
    parts.append(f'<line x1="{x}" y1="{TOP+54}" x2="{x}" y2="{BOT}" class="life"/>')


def call(y, a, b, label, n=None):
    xa, xb = xs[a], xs[b]; d = 8 if xb > xa else -8
    parts.append(f'<path d="M{xa} {y} H{xb-d}" class="call"/>')
    if n:
        parts.append(f'<circle cx="{xa+(d*2.4)}" cy="{y-14}" r="10" fill="#3b6fb6"/><text x="{xa+(d*2.4)}" y="{y-10}" text-anchor="middle" class="num">{n}</text>')
    t((xa + xb) / 2 + (d * 2.4), y - 7, label, 'msg', 'middle')


def ret(y, a, b, label):
    xa, xb = xs[a], xs[b]; d = 8 if xb > xa else -8
    parts.append(f'<path d="M{xa} {y} H{xb-d}" class="ret"/>'); t((xa + xb) / 2, y - 6, label, 'small', 'middle')


def act(i, y0, y1):
    parts.append(f'<rect x="{xs[i]-5}" y="{y0}" width="10" height="{y1-y0}" class="act"/>')


act(1, 190, 295); act(1, 345, 700)
call(190, 0, 1, 'POST 7.6', '1')
call(238, 1, 3, 'active()', '2'); ret(258, 3, 1, '(version, products)')
call(295, 1, 0, '202 PENDING', '3')
parts.append(f'<line x1="150" y1="318" x2="{W-60}" y2="318" class="rule"/>')
t((150 + W - 60) / 2, 313, 'HTTP 끝 — 아래는 Supervisor 워커', 'small', 'middle')

call(355, 1, 2, 'to_internal(body)', '4'); ret(375, 2, 1, 'ProfileRequest')
call(410, 1, 2, 'profile(rq, …)', '5')
act(2, 410, 585)
call(450, 2, 3, 'active()', '6'); ret(470, 3, 2, 'products')
t(615, 505, 'build_pool() → ProfileOutcome(RESULT_READY)', 'mono')
call(540, 2, 4, 'save(outcome)', '7'); ret(560, 4, 2, '')
ret(585, 2, 1, 'ProfileOutcome')

call(625, 1, 5, 'send_profile_callback(outcome)', '8')
act(5, 625, 700)
call(655, 5, 6, 'POST 7.7', '9'); ret(675, 6, 5, '200 · 409 · 400 · 5xx')
ret(700, 5, 1, 'RunStatus')
t(360, 718, 'store.save(마지막 상태)', 'mono')

parts.append('</svg>')
(P / 'v1-flow.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8'); print('svg ok')
