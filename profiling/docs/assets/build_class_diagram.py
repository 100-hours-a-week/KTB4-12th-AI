"""프로파일링 클래스 다이어그램 — schemas.py(바깥 계약) · types.py(내부 자료형) · ports.py(모양) · 구현 · 조립.
python3 build_class_diagram.py → class-diagram.svg (PNG는 Chrome 헤드리스: --headless --screenshot --window-size=2W,2H)
2026-10-07: 필드 목록과 설명문을 걷어내고 클래스 이름 + 핵심 1~2줄만 남겼다. 필드 전체는 schemas.py · types.py 가 정본이다."""
from pathlib import Path
from xml.sax.saxutils import escape

P = Path(__file__).resolve().parent
W, H = 1880, 740
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs>
<marker id="tri" markerWidth="12" markerHeight="12" refX="11" refY="6" orient="auto"><path d="M0 0 L12 6 L0 12 Z" fill="#fff" stroke="#3b6fb6" stroke-width="1.3"/></marker>
<marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#77818b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}
.pkg{{font-size:17px;font-weight:700;fill:#5e6873}}.cn{{font-size:15px;font-weight:700}}.st{{font-size:11.5px;fill:#5e6873}}
.m{{font-family:Menlo,ui-monospace,monospace;font-size:12px;fill:#3f4a54}}.small{{font-size:13px;fill:#5e6873}}
.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.4;stroke-dasharray:6 4;marker-end:url(#tri)}}.use{{fill:none;stroke:#77818b;stroke-width:1.3;marker-end:url(#arr)}}
.conv{{fill:none;stroke:#b8860b;stroke-width:1.5;stroke-dasharray:2 3;marker-end:url(#arr)}}.pkgbg{{fill:#f7f8fa;stroke:#e1e5ea;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']


def t(x, y, s, c='m', anchor=None):
    an = f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')


LH = 17


def cls(x, y, w, name, rows=(), stereo=None, blue=False, yellow=False):
    """UML 상자: 이름(+스테레오타입) / 핵심 줄 1~3. 높이는 내용에 맞춤. 반환 아래 y."""
    head = 36 if stereo else 28
    h = head + (8 + LH * len(rows) + 4 if rows else 0)
    fill = '#f5f8fd' if blue else ('#fffbe6' if yellow else '#fff')
    stroke = '#9db0d2' if blue else ('#e6d98a' if yellow else '#bcc5cd')
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
    if stereo:
        t(x + w / 2, y + 14, stereo, 'st', 'middle'); t(x + w / 2, y + 30, name, 'cn', 'middle')
    else:
        t(x + w / 2, y + 19, name, 'cn', 'middle')
    if rows:
        yy = y + head
        parts.append(f'<line x1="{x}" y1="{yy}" x2="{x+w}" y2="{yy}" stroke="{stroke}"/>')
        for i, r in enumerate(rows):
            t(x + 10, yy + 14 + i * LH, r)
    return y + h


def pkg(x, y, w, h, name):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="pkgbg"/>'); t(x + 12, y + 22, name, 'pkg')


def line(d, c):
    parts.append(f'<path d="{d}" class="{c}"/>')


t(40, 46, '프로파일링 클래스 다이어그램 — 계약 · 내부 자료형 · 포트 · 구현 · 조립', 'title')
t(40, 70, '△ 점선 = 포트 구현 · 황토 점선 = 변환(camelCase ↔ snake_case) · 회색 = 사용', 'small')

BW = 276            # 상자 폭 (패키지 600 안에 둘)
# ---------------- schemas.py (바깥 계약)   x 40..640
pkg(40, 92, 600, 318, 'schemas.py — 바깥 계약 (camelCase)')
cls(56, 124, BW, 'ProductRecord', ['productId · categoryId · viewCount', 'availability (3값)'], '«7.9 상품»', blue=True)
cls(348, 124, BW, 'ProfileExtractRequest', ['recipientUserId · sourceVersion', 'dislikedCategories ≤5 · reviews ≤10'], '«7.6 요청»', blue=True)
cls(56, 226, BW, 'Accepted DTO 2개', ['recipientUserId · sourceVersion', 'profileStatus'], '«7.6 · 7.7 응답 data»', blue=True)
cls(348, 226, BW, 'ProfileCallbackRequest', ['sourceVersion · profileStatus', 'recommendedProductIds ≤30'], '«7.7 요청»', blue=True)
cls(56, 328, 568, 'SuccessResponse[T]  /  ErrorResponse', ['data: T  |  error: {code, traceId}'], blue=True)

# ---------------- types.py (내부)   x 760..1360
pkg(760, 92, 600, 318, 'types.py — 내부 자료형 (snake_case)')
cls(776, 124, BW, 'ProfileRequest', ['recipient_user_id · source_version', 'disliked_categories · reviews'])
cls(1068, 124, BW, 'RunStatus (StrEnum)', ['RUNNING · RESULT_READY · DELIVERED', 'SUPERSEDED · FAILED'])
cls(776, 218, BW, 'ValidationResult', ['disliked_tags · likes · dislikes'])
cls(1068, 218, BW, 'SearchResult', ['product_ids ≤30 · catalog_version_id'])
cls(776, 296, 568, 'ProfileOutcome', ['status: RunStatus · validation · search', 'failure_reason · prompt_version'])

# ---------------- ports.py   x 1400..1840
pkg(1400, 92, 440, 336, 'ports.py — 포트 (typing.Protocol)')
y = 124
for name, fn in [('CatalogReader', 'active() · by_id()'), ('ProfileRunStore', 'save() · get_run() · run_lock()'),
                 ('RecipientProfileStore', 'upsert() · get() · delete()'), ('BackendPort', 'send_profile_callback()')]:
    y = cls(1416, y, 408, name, [fn], '«Protocol»', yellow=True) + 12

# ---------------- 구현   x 1400..1840
pkg(1400, 448, 440, 150, '구현 — 포트에 꽂히는 클래스')
cls(1416, 480, 408, '구현 클래스', ['catalog.py   Db/FileCatalogReader', 'stores.py    DbProfileRunStore · DbRecipientProfileStore', 'backend.py   HttpBackendPort'], '«포트 구현»')
line('M1620 480 V436', 'impl')

# ---------------- tools/fake_backend
pkg(1400, 618, 440, 96, 'tools/fake_backend — 로컬 시험용')
cls(1416, 650, 408, 'app (FastAPI)', ['POST 7.7 (ok/409/400/500/timeout) · GET 7.9'])

# ---------------- main.py · intake.py   x 40..640
pkg(40, 440, 600, 130, 'main.py · intake.py — 조립 · Transport')
cls(56, 472, BW, 'main.app (FastAPI)', ['lifespan() — 구현 4개 + Supervisor', 'app.state · GET /health'], '«app»')
cls(348, 472, BW, 'intake.py', ['extract_and_pool() → 202 PENDING', 'dispatch() · run_and_callback()'], '«router»')

# ---------------- pipeline.py   x 760..1360
pkg(760, 440, 600, 130, 'pipeline.py — 업무')
cls(776, 472, 568, 'pipeline', ['to_internal · needs_model · build_pool', 'profile(rq, catalog, store, …) → ProfileOutcome'], '«module»')

# ---------------- 관계
line('M624 165 H770', 'conv'); t(700, 158, 'to_internal()', 'small', 'middle')          # 7.6 DTO → ProfileRequest
line('M776 333 H700 V267 H630', 'conv'); t(700, 254, 'to_callback()', 'small', 'middle')  # ProfileOutcome → 7.7 DTO
line('M624 513 H770', 'use'); t(697, 506, '호출', 'small', 'middle')                      # intake → pipeline
line('M1344 513 H1372 V250 H1394', 'use'); t(1366, 380, '포트로만', 'small', 'end')         # pipeline → ports
line('M194 554 V600 H1380 V520 H1394', 'use'); t(760, 594, '조립 — 구체 클래스는 main.py 만 안다', 'small')
line('M1620 579 V644', 'use'); t(1630, 616, '7.7', 'small')                               # HttpBackendPort → fake

parts.append('</svg>')
(P / 'class-diagram.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')
print('svg ok')
