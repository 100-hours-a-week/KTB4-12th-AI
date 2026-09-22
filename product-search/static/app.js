const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => new Intl.NumberFormat('ko-KR').format(n);
const defaults = () => ({minPrice:null,maxPrice:null,categoryIds:[],excludeCategoryIds:[],brands:[],excludeBrands:[],productTypes:[],excludeProductIds:[],availability:'any'});
const prefDefaults = () => ({preferredCategoryIds:[],downrankCategoryIds:[]});
const state = {meta:null, filters:defaults(), preferences:prefDefaults(), mode:'hybrid', result:null, query:'', sequence:0,
  controller:null, timer:null, composing:false, cache:new Map(), details:new Map(), contexts:new Map(), ratings:new Map(), detail:null, note:null, rows:[]};
let toastTimer;

function toast(message) { clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,2700); }
async function api(url, body, signal) {
  const response = await fetch(url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,signal});
  const data=await response.json();
  if(!response.ok) throw Error(data.issues?.map(x=>x.message.replace('Value error, ','')).join(' / ') || (typeof data.detail==='string'?data.detail:data.message) || '요청을 처리하지 못했습니다. 다시 시도해 주세요.');
  return data;
}
function request(offset=0) {return {query:$('#query').value.trim(),filters:structuredClone(state.filters),preferences:structuredClone(state.preferences),mode:state.mode,limit:48,offset};}
function categoryName(id) {return state.meta?.categories.find(c=>c.id===id)?.name || id;}
function busy(value) {$('#busy-indicator').hidden=!value;$('#results').setAttribute('aria-busy',String(value));$('#load-more').disabled=value;}
function error(message) {$('#error').textContent=message;$('#error').hidden=!message;}
function schedule(delay=280) {clearTimeout(state.timer);state.timer=setTimeout(()=>run(),delay);}
function skeletons() {$('#results').innerHTML=Array.from({length:12},()=>'<div class="skeleton-card" aria-hidden="true"><div class="skeleton skeleton-image"></div><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line short"></div></div>').join('');}

async function run({append=false,force=false}={}) {
  if(!state.meta)return;
  clearTimeout(state.timer);
  const req=request(append?state.rows.length:0);
  if(req.filters.minPrice!==null && req.filters.maxPrice!==null && req.filters.minPrice>req.filters.maxPrice) {error('최소 가격은 최대 가격보다 클 수 없습니다.');return;}
  const key=JSON.stringify(req), seq=++state.sequence;
  state.controller?.abort();state.controller=new AbortController();error('');
  syncFilters();
  const url=new URL(location);url.search='';if(req.query)url.searchParams.set('q',req.query);history.replaceState(null,'',url);
  if(state.cache.has(key)&&!force) {applyResult(state.cache.get(key),append,true);busy(false);return;}
  busy(true);
  try {
    const start=performance.now();
    const data=await api('/api/search',req,state.controller.signal);
    if(seq!==state.sequence)return;
    data.clientMs=Math.round(performance.now()-start);
    state.cache.set(key,data);if(state.cache.size>50)state.cache.delete(state.cache.keys().next().value);
    applyResult(data,append,false);
  } catch(e) {if(e.name!=='AbortError'&&seq===state.sequence){error(e.message);if(!state.result)$('#results').innerHTML='';}}
  finally {if(seq===state.sequence)busy(false);}
}

function applyResult(data,append=false,localCache=false) {
  state.result=data;state.query=data.request.query;
  if(!append){state.rows=[];state.contexts.clear();$('#results').innerHTML='';}
  const start=state.rows.length;
  for(const p of data.hits){state.contexts.set(p.id,{product:p,searchId:data.searchId,snapshotId:data.snapshotId});state.rows.push(p);}
  $('#results').insertAdjacentHTML('beforeend',data.hits.map((p,i)=>card(p,start+i)).join(''));
  $('#empty-state').hidden=state.rows.length>0;
  $('#load-more').hidden=!data.hasMore;
  $('#result-title').textContent=data.request.query?`“${data.request.query}”`:'전체 상품';
  $('#result-count').textContent=`${money(data.candidateCount)}개`;
  const cached=localCache||data.timing.cached;
  $('#timing').textContent=localCache?'즉시 · 브라우저 캐시':`${Math.round(data.timing.totalMs)}ms${cached?' · 캐시':''}`;
  $('#timing').title=`임베딩 ${data.timing.embeddingMs}ms · 순위 계산 ${data.timing.retrievalMs}ms${data.clientMs?` · 네트워크 포함 ${data.clientMs}ms`:''}`;
  $('#timing').classList.toggle('cached',cached);
  $('#footer-count').textContent=`${money(state.rows.length)}개 표시 · 조건에 맞는 전체 상품 ${money(data.eligibleCount)}개${data.request.query?' · 검색 후보 최대 200개':''}`;
  $('#snapshot').textContent=`카탈로그 ${data.snapshotId.slice(0,8)}`;
  ['#missing-button','#share-button','#export-button'].forEach(id=>$(id).disabled=false);
  $('#live-status').textContent=`${data.candidateCount}개의 상품 후보를 찾았습니다.`;
}

function card(p,index) {
  const context=state.contexts.get(p.id),rating=state.ratings.get(context.searchId+':'+p.id);
  const type={Voucher:'상품권·이용권',Pickup:'픽업'}[p.productType];
  const label=p.matchedFields?.length?p.matchedFields.slice(0,2).join(' · '):(state.query?'의미 유사 후보':p.categoryGroup);
  return `<article class="product-card" data-id="${esc(p.id)}"><button class="card-open" data-action="detail" aria-label="${esc(p.name)} 상세 보기"><div class="product-image"><span class="product-rank">${p.rank}</span><img src="${esc(p.image)}" data-fallback="${esc(p.imageFallback)}" alt="${esc(p.name)}" width="384" height="384" loading="${index<12?'eager':'lazy'}" decoding="async" ${index<2?'fetchpriority="high"':''}>${type?`<span class="product-kind">${type}</span>`:''}</div><div class="product-info"><div class="product-brand">${esc(p.brand)}</div><h2 class="product-name">${esc(p.name)}</h2><p class="product-description">${esc(p.description || '상품 상세에서 정보를 확인하세요.')}</p><div class="product-price-row"><strong class="product-price">${money(p.price)}<small>원</small></strong><span class="product-category">${esc(p.category)}</span></div></div></button><div class="card-bottom"><span class="match-label">${esc(label)}</span><div class="feedback-actions"><button data-action="relevant" class="${rating==='relevant'?'rated':''}" title="관련 있는 상품" aria-label="${esc(p.name)} 관련 있음">✓</button><button data-action="irrelevant" class="${rating==='irrelevant'?'rated':''}" title="관련 없는 상품" aria-label="${esc(p.name)} 관련 없음">✕</button></div></div></article>`;
}

function renderMetadata() {
  $('#catalog-count').textContent=`${money(state.meta.productCount)}개 상품`;
  const groups=Map.groupBy(state.meta.categories,c=>c.group);
  $('#categories').innerHTML=[...groups].map(([group,cs])=>`<details class="category-group"><summary><label><input type="checkbox" data-group="${esc(group)}" aria-label="${esc(group)} 전체 선택"><span>${esc(group)}</span></label><span class="category-count">${money(cs.reduce((a,c)=>a+c.count,0))}</span></summary><div class="category-children">${cs.map(c=>`<label><input type="checkbox" data-category="${esc(c.id)}"><span>${esc(c.name)}</span><span class="category-count">${money(c.count)}</span></label>`).join('')}</div></details>`).join('');
  $('#brands-list').innerHTML=state.meta.brands.map(b=>`<option value="${esc(b.name)}"></option>`).join('');
  const options=[...groups].map(([group,cs])=>`<optgroup label="${esc(group)}">${cs.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</optgroup>`).join('');
  ['#exclude-category','#prefer-category','#downrank-category'].forEach(id=>$(id).insertAdjacentHTML('beforeend',options));
  $('#about-body').innerHTML=`<p>챗봇·프로파일러와 같은 검색 코어로 상품 후보를 확인하는 QA 화면입니다.</p>${[['상품',`${money(state.meta.productCount)}개`],['검색 방식','키워드 + 의미 검색 · RRF'],['임베딩',state.meta.model],['벡터',`${state.meta.dimensions}차원 · 입력 최대 ${state.meta.maxTokens}토큰`],['카탈로그',state.meta.source],['순위 설정',state.meta.algorithm]].map(([k,v])=>`<div class="about-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}<p>표시 가격은 수집 당시 기준이며 현재 판매 상태는 미확인입니다. 검색 순위와 점수는 상품의 적합성을 보장하지 않습니다.</p><a class="about-link" href="/docs" target="_blank" rel="noopener">검색 API 문서 ↗</a>`;
}

function syncFilters() {
  $('#min-price').value=state.filters.minPrice??'';$('#max-price').value=state.filters.maxPrice??'';
  $$('[data-price]').forEach(b=>b.classList.toggle('selected',state.filters.maxPrice===Number(b.dataset.price)&&state.filters.minPrice===null));
  $$('[data-category]').forEach(c=>c.checked=state.filters.categoryIds.includes(c.dataset.category));
  $$('[data-group]').forEach(c=>{const group=state.meta.categories.filter(x=>x.group===c.dataset.group).map(x=>x.id);const n=group.filter(id=>state.filters.categoryIds.includes(id)).length;c.checked=n===group.length;c.indeterminate=n>0&&n<group.length;});
  $$('[name="product-type"]').forEach(c=>c.checked=state.filters.productTypes.includes(c.value));
  $('#exclude-products').value=state.filters.excludeProductIds.join(', ');$('#availability').value=state.filters.availability;
  const chips=[];
  const chip=(label,field,value,pref=false)=>{chips.push({label,field,value,pref});};
  if(state.filters.minPrice!==null||state.filters.maxPrice!==null)chip(`${state.filters.minPrice!==null?money(state.filters.minPrice):'0'} ~ ${state.filters.maxPrice!==null?money(state.filters.maxPrice):'제한 없음'}원`,'price',null);
  state.filters.categoryIds.forEach(v=>chip(categoryName(v),'categoryIds',v));
  state.filters.brands.forEach(v=>chip(v,'brands',v));
  state.filters.excludeBrands.forEach(v=>chip(`${v} 제외`,'excludeBrands',v));
  state.filters.excludeCategoryIds.forEach(v=>chip(`${categoryName(v)} 제외`,'excludeCategoryIds',v));
  state.filters.productTypes.forEach(v=>chip({Shipping:'배송 상품',Voucher:'상품권·이용권',Pickup:'픽업'}[v],'productTypes',v));
  if(state.filters.excludeProductIds.length)chip(`상품 ${state.filters.excludeProductIds.length}개 제외`,'excludeProductIds',null);
  if(state.filters.availability!=='any')chip({available:'판매 가능',unavailable:'판매 불가',unknown:'판매 상태 미확인'}[state.filters.availability],'availability',null);
  state.preferences.preferredCategoryIds.forEach(v=>chip(`${categoryName(v)} 선호`,'preferredCategoryIds',v,true));
  state.preferences.downrankCategoryIds.forEach(v=>chip(`${categoryName(v)} 비선호`,'downrankCategoryIds',v,true));
  state.chips=chips;
  $('#active-filters').innerHTML=chips.map((c,i)=>`<button data-chip="${i}" aria-label="${esc(c.label)} 조건 제거">${esc(c.label)}<span>×</span></button>`).join('');
  $('#filter-count').textContent=chips.length;$('#filter-count').hidden=!chips.length;$('#mobile-filter-count').textContent=chips.length||'';
  for(const [id,field,pref] of [['brand-chips','brands'],['exclude-brand-chips','excludeBrands'],['exclude-category-chips','excludeCategoryIds'],['prefer-category-chips','preferredCategoryIds',true],['downrank-category-chips','downrankCategoryIds',true]]) {
    $(`#${id}`).innerHTML=(pref?state.preferences:state.filters)[field].map(v=>`<button data-remove-field="${field}" data-value="${esc(v)}" ${pref?'data-pref="true"':''}>${esc(field.includes('Category')?categoryName(v):v)}<span>×</span></button>`).join('');
  }
}
function removeFilter(field,value,pref=false){
  const target=pref?state.preferences:state.filters;
  if(field==='price'){target.minPrice=null;target.maxPrice=null;}
  else if(field==='availability')target.availability='any';
  else target[field]=value===null?[]:target[field].filter(x=>x!==value);
  syncFilters();run();
}
function reset(){state.filters=defaults();state.preferences=prefDefaults();syncFilters();run();}
function addBrand(inputId,field){const input=$(inputId),value=input.value.trim();if(!value)return;const brand=state.meta.brands.find(b=>b.name.toLocaleLowerCase()===value.toLocaleLowerCase());if(!brand){toast('목록에 있는 브랜드를 선택해 주세요.');return;}if(!state.filters[field].includes(brand.name))state.filters[field].push(brand.name);input.value='';syncFilters();run();}

async function detail(id) {
  const context=state.contexts.get(id);if(!context)return;
  state.detail=context;
  $('#detail-dialog').showModal();document.body.style.overflow='hidden';
  $('#detail-body').innerHTML='<div class="loading-line">상품 정보를 불러오는 중…</div>';
  try {
    let product=state.details.get(id);
    if(!product){const data=await api('/api/products',{ids:[id],snapshotId:context.snapshotId});product=data.products[0];if(!product)throw Error('상품 정보를 찾을 수 없습니다.');state.details.set(id,product);}
    if(state.detail?.product.id!==id)return;
    const p=product,r=context.product;
    const attrs=Object.entries(p.attributes);
    const labels={size:'크기',specification:'주요 사양',volume:'용량·수량',material:'소재',color:'색상',kind:'종류',usage:'사용 방법',food_type:'식품 유형',composition:'구성',recommended_age:'권장 연령',ingredients:'성분',use_and_form:'용도·형태',functional_information:'기능 정보'};
    $('#detail-body').innerHTML=`<div class="detail-product-head"><img class="detail-image" src="${esc(p.imageLarge)}" data-fallback="${esc(p.imageFallback)}" alt="${esc(p.name)}" width="768" height="768"><div><div class="product-brand">${esc(p.brand)}</div><h2>${esc(p.name)}</h2><div class="detail-price">${money(p.price)}<small> 원</small></div><p class="source-note">수집 당시 가격 · 현재 판매 상태 미확인</p></div></div><div class="detail-links"><a href="${safeUrl(p.productUrl)}" target="_blank" rel="noopener noreferrer">원본 상품 페이지 ↗</a><button data-detail-action="copy-id">상품 ID 복사</button></div><section class="detail-section"><h3>상품 설명</h3><p class="detail-description">${esc(p.description)}</p></section><section class="detail-section"><h3>상품 정보</h3><table class="detail-attributes"><tbody><tr><th>카테고리</th><td>${esc(p.categoryGroup)} › ${esc(p.category)}</td></tr><tr><th>유형</th><td>${{Shipping:'배송 상품',Voucher:'상품권·이용권',Pickup:'픽업'}[p.productType]||'미확인'}</td></tr>${attrs.map(([k,v])=>`<tr><th>${esc(labels[k]||k.replace('source_',''))}</th><td>${esc(v)}</td></tr>`).join('')}</tbody></table>${!attrs.length?'<p class="field-help">추가 속성 자료가 없습니다. 상품명과 설명에서 확인할 수 있는 정보만 표시합니다.</p>':''}</section>${p.tags.length?`<section class="detail-section"><h3>검색 보조 태그</h3><div class="detail-tags">${p.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div></section>`:''}<details class="detail-section"><summary>검색 근거·점수</summary><div class="score-grid"><div class="score-cell"><span>통합 순위 점수</span><strong>${Number(r.score).toFixed(4)}</strong></div><div class="score-cell"><span>키워드 점수</span><strong>${r.lexicalScore.toFixed(2)}</strong><small>${r.lexicalRank?`${r.lexicalRank}위`:''}</small></div><div class="score-cell"><span>의미 유사도</span><strong>${r.denseScore===null?'—':r.denseScore.toFixed(3)}</strong><small>${r.denseRank?`${r.denseRank}위`:''}</small></div></div><p class="source-note">${esc((r.matchedFields||[]).join(' · ')||'단어가 직접 일치한 필드 없음')}<br>점수는 추천 확률이 아닙니다. 키워드·벡터 점수의 단위가 다릅니다.</p></details><details class="detail-section"><summary>실제 검색 문서</summary><p class="source-note">${money(p.tokenCount)}토큰${p.embeddingTruncated?' · 벡터 입력 1,024토큰에서 잘림 · 원문은 아래에 보존':' · 벡터 입력에 전체 포함'}</p><pre class="source-text">${esc(p.sourceText)}</pre></details><section class="detail-section"><h3>이 검색 결과는 어땠나요?</h3><div class="detail-feedback"><button data-detail-action="relevant">✓ 관련 있음</button><button data-detail-action="irrelevant">✕ 관련 없음</button><button data-detail-action="filter_violation">조건 위반 제보</button></div></section>`;
  }catch(e){$('#detail-body').innerHTML=`<div class="error-banner">${esc(e.message)}</div>`;}
}
function safeUrl(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#';}catch{return '#';}}
function closeDetail(){$('#detail-dialog').close();document.body.style.overflow='';state.detail=null;}
async function rate(context,verdict,note='') {
  try {await api('/api/feedback',{searchId:context.searchId,productId:context.product?.id??null,verdict,note});state.ratings.set(context.searchId+':'+context.product?.id,verdict);const cardEl=$$('.product-card').find(c=>c.dataset.id===context.product?.id);if(cardEl)$$('.feedback-actions button',cardEl).forEach(b=>b.classList.toggle('rated',b.dataset.action===verdict));toast('QA 의견을 저장했습니다.');return true;}catch(e){toast(e.message);return false;}
}
function openNote(context,verdict){state.note={context,verdict};$('#note-title').textContent=verdict==='missing'?'누락된 상품 제보':'검색 조건 위반 제보';$('#note-description').textContent=verdict==='missing'?'나와야 하는 상품명이나 기대한 결과를 남겨 주세요.':context.product.name;$('#note-text').value='';$('#note-dialog').showModal();$('#note-text').focus();}
async function copy(text){try{await navigator.clipboard.writeText(text);toast('복사했습니다.');}catch{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();document.execCommand('copy');input.remove();toast('복사했습니다.');}}
function openFilters(open){$('#filters-panel').classList.toggle('open',open);$('#filter-scrim').hidden=!open;document.body.style.overflow=open?'hidden':'';}

$('#search-form').addEventListener('submit',e=>{e.preventDefault();run();$('#query').blur();});
$('#query').addEventListener('compositionstart',()=>state.composing=true);
$('#query').addEventListener('compositionend',()=>{state.composing=false;schedule(350);});
$('#query').addEventListener('input',()=>{if(!state.composing)schedule(350);});
$$('[data-query]').forEach(b=>b.addEventListener('click',()=>{$('#query').value=b.dataset.query;run();}));
document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&!$('dialog[open]')){e.preventDefault();$('#query').focus();}if(e.key==='Escape')openFilters(false);});
$('#min-price').addEventListener('input',e=>{state.filters.minPrice=e.target.value===''?null:Number(e.target.value);schedule(450);});
$('#max-price').addEventListener('input',e=>{state.filters.maxPrice=e.target.value===''?null:Number(e.target.value);schedule(450);});
$$('[data-price]').forEach(b=>b.addEventListener('click',()=>{state.filters.minPrice=null;state.filters.maxPrice=Number(b.dataset.price);syncFilters();run();}));
$('#categories').addEventListener('change',e=>{const el=e.target;if(el.dataset.category){state.filters.categoryIds=el.checked?[...new Set([...state.filters.categoryIds,el.dataset.category])]:state.filters.categoryIds.filter(x=>x!==el.dataset.category);}else if(el.dataset.group){const ids=state.meta.categories.filter(c=>c.group===el.dataset.group).map(c=>c.id);state.filters.categoryIds=el.checked?[...new Set([...state.filters.categoryIds,...ids])]:state.filters.categoryIds.filter(id=>!ids.includes(id));}syncFilters();run();});
$$('[name="product-type"]').forEach(c=>c.addEventListener('change',()=>{state.filters.productTypes=$$('[name="product-type"]:checked').map(c=>c.value);run();}));
$('#add-brand').addEventListener('click',()=>addBrand('#brand-input','brands'));
$('#add-exclude-brand').addEventListener('click',()=>addBrand('#exclude-brand','excludeBrands'));
for(const [input,field] of [['#brand-input','brands'],['#exclude-brand','excludeBrands']]){$(input).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();addBrand(input,field);}});$(input).addEventListener('change',()=>addBrand(input,field));}
for(const [id,field,pref] of [['#exclude-category','excludeCategoryIds'],['#prefer-category','preferredCategoryIds',true],['#downrank-category','downrankCategoryIds',true]]){$(id).addEventListener('change',e=>{if(!e.target.value)return;const target=pref?state.preferences:state.filters;if(!target[field].includes(e.target.value))target[field].push(e.target.value);e.target.value='';syncFilters();run();});}
$('#exclude-products').addEventListener('change',e=>{state.filters.excludeProductIds=[...new Set(e.target.value.split(/[,\s]+/).filter(Boolean))];run();});
$('#availability').addEventListener('change',e=>{state.filters.availability=e.target.value;run();});
$('#active-filters').addEventListener('click',e=>{const b=e.target.closest('[data-chip]');if(b){const c=state.chips[Number(b.dataset.chip)];removeFilter(c.field,c.value,c.pref);}});
$('#filters-panel').addEventListener('click',e=>{const b=e.target.closest('[data-remove-field]');if(b)removeFilter(b.dataset.removeField,b.dataset.value,Boolean(b.dataset.pref));});
$('#reset-filters').addEventListener('click',reset);$('#empty-reset').addEventListener('click',reset);
$$('[data-mode]').forEach(b=>b.addEventListener('click',()=>{state.mode=b.dataset.mode;$$('[data-mode]').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-pressed',String(x===b));});run();}));
for(const [id,list] of [['#grid-view',false],['#list-view',true]]){$(id).addEventListener('click',()=>{$('#results').classList.toggle('list-layout',list);$('#grid-view').classList.toggle('active',!list);$('#list-view').classList.toggle('active',list);$('#grid-view').setAttribute('aria-pressed',String(!list));$('#list-view').setAttribute('aria-pressed',String(list));});}
$('#load-more').addEventListener('click',()=>run({append:true}));
$('#results').addEventListener('click',e=>{const b=e.target.closest('[data-action]'),cardEl=e.target.closest('[data-id]');if(!b||!cardEl)return;const context=state.contexts.get(cardEl.dataset.id);if(b.dataset.action==='detail')detail(cardEl.dataset.id);else rate(context,b.dataset.action);});
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&e.target.dataset.fallback){const img=e.target;const fallback=img.dataset.fallback;delete img.dataset.fallback;img.src=fallback;}},true);
$('#close-detail').addEventListener('click',closeDetail);
$('#detail-dialog').addEventListener('cancel',()=>{document.body.style.overflow='';state.detail=null;});
$('#detail-dialog').addEventListener('click',e=>{if(e.target===$('#detail-dialog')&&e.clientX<$('#detail-dialog').getBoundingClientRect().left)closeDetail();});
$('#detail-body').addEventListener('click',e=>{const action=e.target.closest('[data-detail-action]')?.dataset.detailAction;if(!action||!state.detail)return;if(action==='copy-id')copy(state.detail.product.id);else if(action==='filter_violation')openNote(state.detail,action);else rate(state.detail,action);});
$('#detail-exclude').addEventListener('click',()=>{if(!state.detail)return;state.filters.excludeProductIds=[...new Set([...state.filters.excludeProductIds,state.detail.product.id])];closeDetail();syncFilters();run();});
$('#missing-button').addEventListener('click',()=>{if(state.result)openNote({searchId:state.result.searchId},'missing');});
$('#close-note').addEventListener('click',()=>$('#note-dialog').close());$('#cancel-note').addEventListener('click',()=>$('#note-dialog').close());
$('#note-form').addEventListener('submit',async e=>{e.preventDefault();const button=$('[type=submit]',$('#note-form'));button.disabled=true;try{if(await rate(state.note.context,state.note.verdict,$('#note-text').value.trim()))$('#note-dialog').close();}finally{button.disabled=false;}});
$('#share-button').addEventListener('click',()=>{if(state.result)copy(`${location.origin}/?run=${state.result.searchId}`);});
$('#export-button').addEventListener('click',()=>{if(!state.result)return;const blob=new Blob([JSON.stringify(state.result,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`search-${state.result.searchId.slice(0,8)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});
$('#about-button').addEventListener('click',()=>$('#about-dialog').showModal());$('#close-about').addEventListener('click',()=>$('#about-dialog').close());
$('#open-filters').addEventListener('click',()=>openFilters(true));$('#close-filters').addEventListener('click',()=>openFilters(false));$('#filter-scrim').addEventListener('click',()=>openFilters(false));

async function init(){
  skeletons();
  const params=new URLSearchParams(location.search);
  if(params.has('q'))$('#query').value=params.get('q');
  const filterInputs=$$('input,button,select,textarea',$('#filters-panel'));
  filterInputs.forEach(el=>el.disabled=true);
  try {
    state.meta=await api('/api/metadata');renderMetadata();
    filterInputs.forEach(el=>el.disabled=false);
    if(params.has('run')){
      const result=await api(`/api/searches/${encodeURIComponent(params.get('run'))}`);
      $('#query').value=result.request.query;state.filters={...defaults(),...result.request.filters};state.preferences={...prefDefaults(),...result.request.preferences};state.mode=result.request.mode;
      $$('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===state.mode);b.setAttribute('aria-pressed',String(b.dataset.mode===state.mode));});
      syncFilters();applyResult(result);busy(false);
      if(result.snapshotId!==state.meta.snapshotId)error('저장된 검색 결과입니다. 현재 카탈로그와 버전이 달라 다시 검색하면 결과가 바뀔 수 있습니다.');
    }else{await run();}
  }catch(e){error(e.message);$('#results').innerHTML='';busy(false);}
}
init();
