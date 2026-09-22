"""프로파일링 클래스 다이어그램 — schemas.py(바깥 계약) · types.py(내부 자료형) · ports.py(모양) · adapters(구현) · api/main(조립).
python3 build_class_diagram.py → class-diagram.svg (PNG는 Chrome 헤드리스). 필드는 현재 코드 기준 — 바뀌면 여기도 고친다."""
from pathlib import Path
from xml.sax.saxutils import escape
P=Path(__file__).resolve().parent; W,H=1600,1140
parts=[f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W*2}" height="{H*2}" viewBox="0 0 {W} {H}" role="img">
<defs>
<marker id="tri" markerWidth="12" markerHeight="12" refX="11" refY="6" orient="auto"><path d="M0 0 L12 6 L0 12 Z" fill="#fff" stroke="#3b6fb6" stroke-width="1.3"/></marker>
<marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#77818b"/></marker>
<marker id="dia" markerWidth="12" markerHeight="8" refX="11" refY="4" orient="auto"><path d="M0 4 L6 0 L12 4 L6 8 Z" fill="#fff" stroke="#77818b" stroke-width="1.2"/></marker>
<style>text{{font-family:"Apple SD Gothic Neo","Noto Sans KR",Arial,sans-serif;fill:#18212b}}.title{{font-size:30px;font-weight:700;letter-spacing:-.6px}}.pkg{{font-size:17px;font-weight:700;fill:#5e6873}}.cn{{font-size:14.5px;font-weight:700}}.st{{font-size:11.5px;fill:#5e6873}}.m{{font-family:Menlo,ui-monospace,monospace;font-size:11.5px;fill:#3f4a54}}.small{{font-size:13px;fill:#5e6873}}
.impl{{fill:none;stroke:#3b6fb6;stroke-width:1.4;stroke-dasharray:6 4;marker-end:url(#tri)}}.use{{fill:none;stroke:#77818b;stroke-width:1.3;marker-end:url(#arr)}}.has{{fill:none;stroke:#77818b;stroke-width:1.3;marker-end:url(#dia)}}.conv{{fill:none;stroke:#b8860b;stroke-width:1.4;stroke-dasharray:2 3;marker-end:url(#arr)}}.pkgbg{{fill:#f7f8fa;stroke:#e1e5ea;stroke-width:1}}.rule{{stroke:#d9dee3;stroke-width:1}}</style></defs><rect width="{W}" height="{H}" fill="white"/>''']
def t(x,y,s,c='m',anchor=None):
    an=f' text-anchor="{anchor}"' if anchor else ''
    parts.append(f'<text x="{x}" y="{y}" class="{c}"{an}>{escape(s)}</text>')
LH=15
def cls(x,y,w,name,attrs,methods=(),stereo=None,blue=False,yellow=False):
    """UML 상자: 이름(+스테레오타입) / 속성 / 메서드. 높이는 내용에 맞춤. 반환 (x,y,w,h)."""
    head=34 if stereo else 26
    h=head+8+LH*len(attrs)+(8+LH*len(methods) if methods else 0)+6
    fill='#f5f8fd' if blue else ('#fffbe6' if yellow else '#fff'); stroke='#9db0d2' if blue else ('#e6d98a' if yellow else '#bcc5cd')
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
    if stereo: t(x+w/2,y+14,stereo,'st','middle'); t(x+w/2,y+29,name,'cn','middle')
    else: t(x+w/2,y+18,name,'cn','middle')
    yy=y+head; parts.append(f'<line x1="{x}" y1="{yy}" x2="{x+w}" y2="{yy}" stroke="{stroke}"/>')
    for i,a in enumerate(attrs): t(x+8,yy+13+i*LH,a)
    if methods:
        yy=yy+8+LH*len(attrs); parts.append(f'<line x1="{x}" y1="{yy}" x2="{x+w}" y2="{yy}" stroke="{stroke}"/>')
        for i,a in enumerate(methods): t(x+8,yy+13+i*LH,a)
    return (x,y,w,h)
def pkg(x,y,w,h,name): parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" class="pkgbg"/>'); t(x+12,y+22,name,'pkg')
def line(d,c): parts.append(f'<path d="{d}" class="{c}"/>')

t(40,46,'프로파일링 클래스 다이어그램 — 계약 · 내부 자료형 · 포트 · adapter · 조립','title')
t(40,72,'«…구현» = Protocol 구현(상속 없음, 모양만 일치)   회색 실선 → = 사용/호출   회색 ◇ = 보유   황토 점선 → = 변환 함수(camelCase ↔ snake_case)','small')

# ---------------- schemas.py (바깥 계약)  x 40..510
pkg(40,92,470,630,'schemas.py — 바깥 계약 (camelCase · forbid)')
cls(56,124,250,'ProfileExtractRequest',['recipientUserId: int >0','sourceVersion: int ≥0','dislikedCategories: list[…]  (≤5?)','giftPreference: str | None','reviews: list[ReviewDto] ≤10'],stereo='«7.6 요청»',blue=True)
cls(56,258,150,'DislikedCategoryDto',['categoryId: int >0','categoryName: str'],blue=True)
cls(222,258,150,'ReviewDto',['productId: int >0','rating: int 1..5','reviewText: str|None'],blue=True)
cls(330,360,164,'ProfileAccepted',['recipientUserId','sourceVersion','profileStatus="PENDING"'],stereo='«7.6 응답 data»',blue=True)
cls(56,360,250,'ProfileCallbackRequest',['recipientUserId: int >0','sourceVersion: int ≥0','profileStatus = "COMPLETED"','recommendedProductIds: list[int] ≤30','(태그 없음 — DR-035)'],stereo='«7.7 요청»',blue=True)
cls(56,500,150,'ProfileCallbackAccepted',['recipientUserId','sourceVersion','profileStatus'],stereo='«7.7 응답 data»',blue=True)
cls(330,585,164,'ProductRecord',['productId · name · brand','description: str|None','categoryId · categoryName','price · available','updatedAt'],stereo='«7.9 상품»',blue=True)
cls(222,500,272,'SuccessResponse[T]  /  ErrorResponse',['message: str','data: T   |   error: ErrorBody{code, traceId}'],blue=True)
line('M131 258 V246','has'); line('M297 258 V246','has')   # ExtractRequest ◇ Dto들

# ---------------- types.py (내부)  x 560..990
pkg(560,92,430,630,'types.py — 내부 자료형 (snake_case)')
cls(576,124,190,'ProfileRequest',['recipient_user_id: int','source_version: int','gift_preference: str | None','disliked_categories: list','reviews: list[Review]'])
cls(790,124,180,'DislikedCategory',['category_id: int','category_name: str'])
cls(790,196,180,'Review',['product_id · rating','review_text: str | None'])
cls(576,258,190,'ValidationResult',['likes: list[ValidatedTag]','key_features: list[ValidatedTag]','dislikes: list[ValidatedTag]','preferred_tags: list[str]','disliked_tags: list[str]','log: list[Decision]'])
cls(790,258,180,'SearchResult',['product_ids: list[int] ≤30','query_text: str','catalog_version_id: int'])
cls(790,350,180,'RunStatus (StrEnum)',['RUNNING · RESULT_READY','DELIVERED · SUPERSEDED','FAILED'])
cls(576,420,190,'ProfileOutcome',['recipient_user_id','source_version','status: RunStatus','validation: ValidationResult|None','search: SearchResult|None','failure_reason: str | None','prompt_version · validator_version'])
cls(790,470,180,'(v3) DraftItem · ExtractDraft',['ValidatedTag · Decision','— LLM 초안·판정 로그'])
line('M766 150 H783','has'); line('M766 165 H778 V222 H783','has')          # ProfileRequest ◇
line('M676 420 V398','has')                                                  # ValidationResult ◇ Outcome
line('M766 470 H778 V300 H783','has'); line('M766 485 H778 V395 H783','has')  # Outcome ◇ SearchResult · RunStatus
# 변환 함수 (황토 점선) — 두 패키지 사이
line('M306 150 H570','conv'); t(438,143,'pipeline.to_internal()','small','middle')
line('M576 470 H312','conv'); t(444,463,'backend.to_callback()','small','middle')

# ---------------- ports.py  x 1040..1560
pkg(1040,92,520,392,'ports.py — 포트 (typing.Protocol)')
cls(1056,124,330,'CatalogReader',[],['active() → (version_id, list[ProductRecord])','by_id(product_id) → ProductRecord | None'],stereo='«Protocol»',yellow=True)
cls(1400,124,144,'NoActiveCatalog',['(Exception)','7.6에서 503으로'])
cls(1056,222,330,'ProfileRunStore',[],['save(outcome: ProfileOutcome) → None','get(recipient_user_id) → ProfileOutcome | None'],stereo='«Protocol»',yellow=True)
cls(1056,320,330,'BackendPort',[],['send_profile_callback(outcome) → RunStatus'],stereo='«Protocol»',yellow=True)
cls(1400,222,144,'(v3) ProfileModel',[],['analyze(prompt,','  schema, seed)','→ ExtractDraft'],stereo='«Protocol»',yellow=True)
cls(1400,320,144,'(v3) Embedder',[],['embed_docs()','embed_query()'],stereo='«Protocol»',yellow=True)
t(1256,470,'ProductRecord(schemas) · ProfileOutcome(types)를 인자·반환으로 씀 → 회색 실선은 생략','st','middle')

# ---------------- adapters  x 1040..1560
pkg(1040,510,520,190,'adapters/ — 구현 (바깥과 닿는 코드). 스테레오타입이 어느 포트를 구현하는지')
cls(1056,542,150,'FileCatalogReader',['path: Path','_products: list','_by_id: dict'],['active()','by_id()'],stereo='«CatalogReader 구현»')
cls(1216,542,180,'MemoryProfileRunStore',['_items: dict[int,Outcome]','_lock: threading.Lock'],['save()','get()'],stereo='«ProfileRunStore 구현»')
cls(1406,542,140,'HttpBackendPort',['base_url · token','timeout_s','_client: httpx.Client'],['send_profile_callback()','close()'],stereo='«BackendPort 구현»')

# ---------------- pipeline.py  x 560..990
pkg(560,730,430,190,'pipeline.py — 업무 (함수 모듈)')
cls(576,762,400,'pipeline',[],['to_internal(ProfileExtractRequest) → ProfileRequest','needs_model(ProfileRequest) → bool                     (결정 c)','build_pool(rq, products, pool_size) → SearchResult    (결정 a)','profile(rq, *, catalog: CatalogReader, store: ProfileRunStore)','   → ProfileOutcome'],stereo='«module»')
line('M976 830 H1030 V300 H1050','use')
line('M676 762 V700','use'); t(690,720,'types 생성','small')

# ---------------- api / main  x 40..510
pkg(40,730,470,190,'transport · runtime · main — Transport · Supervisor · 조립')
cls(56,762,214,'main.app (FastAPI)',['state.catalog: CatalogReader','state.store: ProfileRunStore','state.backend: BackendPort','state.supervisor: Supervisor'],['lifespan()  adapter 생성 1회','exception_handler 422→400','GET /health'],stereo='«app»')
cls(286,762,208,'intake.py (Transport)',['EXTRACT_AND_POOL_PATH'],['require_service_token()  401','extract_and_pool()  202 / 503','  → supervisor.submit(run_and_callback)','run_and_callback()  → 7.7'],stereo='«router»')
line('M270 810 H279','has')
line('M494 840 H570','use'); t(532,834,'호출','small','middle')
line('M160 700 V690','use'); t(170,712,'DTO 검증·응답','small')
line('M156 894 V915 H1010 V640 H1040','has'); t(600,930,'main.lifespan: 구체 adapter를 만들어 app.state에 보유 (조립은 여기서만)','small')

# ---------------- tools/fake_backend
cls(1056,760,300,'tools/fake_backend.app  «시험용 FastAPI»',['MODE: ok|409|400|500|timeout','_received: list[dict]'],['POST /api/internal/v1/recipients/{id}/profile','GET /received · DELETE /received'])
line('M1471 700 V740 H1206 V753','use'); t(1340,733,'httpx POST 7.7','small')

parts.append(f'<line x1="40" y1="{H-70}" x2="{W-40}" y2="{H-70}" class="rule"/>')
t(40,H-44,'의존 방향: transport → pipeline → ports ←(구현)— adapters.  업무 코드는 adapters를 import하지 않고, 조립(main.py)만 구체 클래스를 안다. 내일 DB adapter(SQLAlchemy)는 ProfileRunStore·CatalogReader를 그대로 구현해 main.py 세 줄만 교체.','small')
t(40,H-22,'v3 추가: ProfileModel(Ollama·규칙 추출기) · Embedder(BGE-m3-ko) · Search 호출 — pipeline.profile() 안의 needs_model 분기 뒤에 붙는다. 자료형은 types.py의 DraftItem·ExtractDraft·ValidatedTag·Decision.','small')
parts.append('</svg>')
(P/'class-diagram.svg').write_text('\n'.join(parts)+'\n',encoding='utf-8'); print('svg ok')
