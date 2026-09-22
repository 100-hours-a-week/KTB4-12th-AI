"""v1 프로파일링 한 건 — 파일 사이의 동작을 시퀀스로. python3 build_v1_flow.py → v1-flow.svg (PNG는 Chrome 헤드리스)"""
from pathlib import Path
from xml.sax.saxutils import escape
P=Path(__file__).resolve().parent; W,H=1460,1030
parts=[f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#3b6fb6"/></marker>
<marker id="r" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#77818b"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}.head{{font-size:15px;font-weight:700}}.sub{{font-size:12.5px;fill:#5e6873}}.msg{{font-size:13.5px;fill:#18212b}}.mono{{font-family:Menlo,ui-monospace,monospace;font-size:12.5px;fill:#3f4a54}}.small{{font-size:13px;fill:#5e6873}}.num{{font-size:12px;font-weight:700;fill:#fff}}
.life{{stroke:#c9d0d6;stroke-width:1.2;stroke-dasharray:4 4}}.call{{fill:none;stroke:#3b6fb6;stroke-width:1.7;marker-end:url(#a)}}.ret{{fill:none;stroke:#77818b;stroke-width:1.4;stroke-dasharray:5 4;marker-end:url(#r)}}.act{{fill:#dfe8f5;stroke:#9db0d2;stroke-width:1}}.note{{fill:#fffbe6;stroke:#e6d98a;stroke-width:1}}.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']
def t(x,y,s,c='msg',anchor=None):
    an=f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')
# lifelines
L=[('Backend',['(내일: 실제 · 오늘: curl)']),('intake.py',['Transport · 7.6 접수','run_and_callback() (Supervisor 슬롯)']),('pipeline.py',['to_internal · needs_model','build_pool · profile()']),
   ('catalog.py',['CatalogReader (파일)']),('stores.py',['ProfileRunStore (메모리)']),('backend.py',['BackendPort (HTTP)']),('tools/fake_backend',['7.7 수신'])]
xs=[110,350,600,830,1020,1210,1380]
t(40,48,'v1 프로파일링 한 건 — 파일 사이의 호출 순서 (7.6 접수 → 202 → 백그라운드 → 7.7 콜백)','title')
t(40,74,'실선 파랑 = 호출, 점선 회색 = 반환. 노란 상자 = 그 시점에 쓰는 자료형(schemas.py = 바깥 계약 camelCase · types.py = 내부 snake_case). 조립(main.py)은 맨 아래.','small')
TOP=100; BOT=905
for (n,subs),x in zip(L,xs):
    w=190 if x not in (110,1330) else 150
    parts.append(f'<rect x="{x-w/2}" y="{TOP}" width="{w}" height="{44+14*len(subs)}" rx="4" fill="#fff" stroke="#bcc5cd" stroke-width="1.3"/>')
    t(x,TOP+22,n,'head','middle')
    for i,s in enumerate(subs):t(x,TOP+40+i*14,s,'sub','middle')
    parts.append(f'<line x1="{x}" y1="{TOP+44+14*len(subs)}" x2="{x}" y2="{BOT}" class="life"/>')
def call(y,a,b,label,n=None,below=None,below_left=False):
    xa,xb=xs[a],xs[b]; d=8 if xb>xa else -8
    parts.append(f'<path d="M{xa} {y} H{xb-d}" class="call"/>')
    if n: parts.append(f'<circle cx="{xa+(d*2.2)}" cy="{y-13}" r="10" fill="#3b6fb6"/><text x="{xa+(d*2.2)}" y="{y-9}" text-anchor="middle" class="num">{n}</text>')
    t((xa+xb)/2+(d*2.2),y-6,label,'msg','middle')
    if below:t(min(xa,xb)+12 if below_left else (xa+xb)/2,y+15,below,'mono',None if below_left else 'middle')
def ret(y,a,b,label):
    xa,xb=xs[a],xs[b]; d=8 if xb>xa else -8
    parts.append(f'<path d="M{xa} {y} H{xb-d}" class="ret"/>'); t((xa+xb)/2,y-5,label,'small','middle')
def act(i,y0,y1):parts.append(f'<rect x="{xs[i]-5}" y="{y0}" width="10" height="{y1-y0}" class="act"/>')
def note(x,y,w,lines):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{14+15*len(lines)}" rx="3" class="note"/>')
    for i,s in enumerate(lines):t(x+8,y+15+i*15,s,'mono')

act(1,200,335); act(1,380,800)
call(200,0,1,'POST 7.6 extract-and-pool','1','{recipientUserId, sourceVersion, dislikedCategories[≤5], giftPreference: null, reviews: []}',True)
note(360,228,320,['body → ProfileExtractRequest  (schemas.py)','위반 → 400 INVALID_REQUEST · 토큰 → 401/403'])
call(290,1,3,'catalog.active() 있는가?  없으면 503','2')
ret(308,3,1,'(version_id, products)')
call(335,1,0,'202 SuccessResponse[ProfileAccepted]','3'); t(120,352,'profileStatus = PENDING','mono')
t(350,372,'── 여기서 HTTP 응답 끝. 아래는 Supervisor 슬롯 안의 백그라운드 (같은 프로세스, 응답과 무관) ──','small','middle')

call(400,1,2,'to_internal(body)','4'); ret(418,2,1,'ProfileRequest  (types.py, snake_case)')
call(450,1,2,'profile(rq, catalog=, store=)','5')
act(2,450,690)
note(615,462,330,['needs_model(rq) → False   (결정 c: 취향 null · 리뷰 [])','→ extract · validate 건너뜀'])
call(530,2,3,'active()','6'); ret(548,3,2,'(1, [ProductRecord…])')
note(615,562,420,['validation = ValidationResult(disliked_tags=비선호 이름, 나머지 [])','search = build_pool(products, 비선호 제외, 30)  (결정 a)','outcome = ProfileOutcome(RESULT_READY, validation, search)'])
call(650,2,4,'save(outcome)','7'); ret(668,4,2,'')
ret(690,2,1,'ProfileOutcome')
note(360,705,330,['if outcome.status == RESULT_READY  → 콜백','FAILED 이면 콜백 없음 (AI는 침묵, Backend가 판정)'])
call(775,1,5,'send_profile_callback(outcome)','8')
act(5,775,880)
note(1000,788,330,['to_callback(outcome) → ProfileCallbackRequest','태그 없음 · recommendedProductIds ≤30  (DR-035)'])
call(848,5,6,'POST 7.7','9')
ret(866,6,5,'200 / 409 / 400 / 5xx')
ret(884,5,1,'RunStatus  DELIVERED · SUPERSEDED · FAILED · RESULT_READY(재시도 대상)')
t(350,900,'log.info("7.7 콜백 결과 … → DELIVERED")   ※ 실행 기록 상태 갱신·재시도는 DB adapter 뒤(#23)','small','middle')

parts.append(f'<line x1="40" y1="{BOT+14}" x2="{W-40}" y2="{BOT+14}" class="rule"/>')
t(40,BOT+40,'조립 (main.py, 시작 1회):  lifespan에서 FileCatalogReader(settings.CATALOG_FILE) · MemoryProfileRunStore() · HttpBackendPort(settings.BACKEND_BASE_URL, token)를 만들어 app.state에 두고, 라우터는 Depends로 꺼내 pipeline에 넘긴다.','small')
t(40,BOT+62,'의존 방향:  api → pipeline → ports(Protocol 모양)  ←구현─ adapters.   pipeline은 adapters 파일을 import하지 않는다. 테스트(tests/unit)는 ports 모양의 가짜를 넣어 5·6·7만 돌린다.','small')
t(40,BOT+84,'v3에서 바뀌는 곳: 5단계 안에서 needs_model → True면 카탈로그 조인 → ProfileModel.analyze() → validator → Search. 1~4, 7~9는 그대로.','small')
parts.append('</svg>')
(P/'v1-flow.svg').write_text('\n'.join(parts)+'\n',encoding='utf-8'); print('svg ok')
