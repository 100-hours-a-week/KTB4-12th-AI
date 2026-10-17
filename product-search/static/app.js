import {parseJSON, stringifyJSON, parseProductId, parseCategoryId} from './json.js';
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => new Intl.NumberFormat('ko-KR').format(n);
const defaults = () => ({minPrice:null,maxPrice:null,categoryIds:[],excludeCategoryIds:[],brands:[],excludeBrands:[],productTypes:[],excludeProductIds:[],availability:'any'});
const prefDefaults = () => ({preferredCategoryIds:[],downrankCategoryIds:[]});
const state = {meta:null, filters:defaults(), preferences:prefDefaults(), mode:'hybrid', result:null, query:'', sequence:0,
  controller:null, timer:null, composing:false, cache:new Map(), details:new Map(), contexts:new Map(), ratings:new Map(), detail:null, note:null, rows:[], snapshotMismatch:false, shareSearchId:null};
let toastTimer;
let filtersOpen=false;

function toast(message) { clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,2700); }
async function api(url, body, signal) {
  const response = await fetch(url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:undefined,body:body?stringifyJSON(body):undefined,signal});
  const data=parseJSON(await response.text());
  if(!response.ok){
    const issues=data.error?.issues??data.issues??[];
    const details=issues.map(x=>`${x.field?x.field+': ':''}${x.message.replace('Value error, ','')}`).join(' / ');
    const message=data.message||(typeof data.detail==='string'?data.detail:'요청을 처리하지 못했습니다. 다시 시도해 주세요.');
    const failure=Error(`${data.error?.code?'['+data.error.code+'] ':''}${message}${details?' · '+details:''}`);
    failure.code=data.error?.code;failure.issues=issues;failure.status=response.status;throw failure;
  }
  return data;
}
function request(offset=0) {return {query:$('#query').value.trim(),filters:structuredClone(state.filters),preferences:structuredClone(state.preferences),mode:state.mode,limit:48,offset};}
function nextOffset(result) {
  if(!result)return null;
  // Older saved QA responses predate nextOffset.
  return Object.hasOwn(result,'nextOffset')?result.nextOffset:result.hasMore?result.request.offset+result.hits.length:null;
}
function categoryName(id) {
  const group=state.meta?.groups?.find(c=>String(c.id)===String(id));
  return group?`${group.name} 전체`:state.meta?.categories.find(c=>String(c.id)===String(id))?.name || id;
}
function categoryChildren(parentId) {return state.meta.categories.filter(c=>String(c.parentId)===String(parentId));}
function categorySelected(id,field='categoryIds') {
  const category=state.meta.categories.find(c=>String(c.id)===String(id));
  return state.filters[field].includes(id)||(category && state.filters[field].includes(category.parentId));
}
function selectCategory(id,checked,group=false,field='categoryIds') {
  let selected=state.filters[field];
  if(group){
    const children=categoryChildren(id).map(c=>c.id);
    selected=selected.filter(value=>value!==id&&!children.includes(value));
    if(checked)selected.push(id);
  }else{
    const category=state.meta.categories.find(c=>c.id===id);
    if(!checked && category && selected.includes(category.parentId)){
      selected=selected.filter(value=>value!==category.parentId);
      selected.push(...categoryChildren(category.parentId).filter(c=>c.id!==id).map(c=>c.id));
    }
    selected=checked?[...selected,id]:selected.filter(value=>value!==id);
  }
  selected=[...new Set(selected)];
  for(const parent of state.meta.groups??[]){
    const children=categoryChildren(parent.id).map(c=>c.id);
    if(children.length&&(selected.includes(parent.id)||children.every(child=>selected.includes(child)))){
      selected=selected.filter(value=>!children.includes(value)&&value!==parent.id);selected.push(parent.id);
    }
  }
  state.filters[field]=selected;
}
function busy(value) {$('#busy-indicator').hidden=!value;$('#results').setAttribute('aria-busy',String(value));$('#load-more').disabled=value;}
function error(message) {$('#error').textContent=message;$('#error').hidden=!message;}
function schedule(delay=280) {clearTimeout(state.timer);state.timer=setTimeout(()=>run(),delay);}
function catalogChanged() {
  state.snapshotMismatch=true;state.cache.clear();state.details.clear();
  $('#load-more').hidden=true;
  error('카탈로그 버전이 달라 이어볼 수 없습니다. 검색 버튼을 눌러 처음부터 다시 검색하세요.');
}
function skeletons() {$('#results').innerHTML=Array.from({length:12},()=>'<div class="skeleton-card" aria-hidden="true"><div class="skeleton skeleton-image"></div><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line short"></div></div>').join('');}

async function run({append=false,force=false,initial=null}={}) {
  if(!state.meta)return;
  clearTimeout(state.timer);
  if(append && (state.snapshotMismatch || state.result?.snapshotId!==state.meta.snapshotId)){catalogChanged();return;}
  const offset=append?nextOffset(state.result):0;
  if(offset===null)return;
  const req=request(offset);
  if(req.filters.minPrice!==null && req.filters.maxPrice!==null && req.filters.minPrice>req.filters.maxPrice) {error('최소 가격은 최대 가격보다 클 수 없습니다.');return;}
  const key=stringifyJSON(req), seq=++state.sequence;
  force=force||state.snapshotMismatch;
  state.controller?.abort();state.controller=new AbortController();error('');
  syncFilters();
  const url=new URL(location);url.search='';if(req.query)url.searchParams.set('q',req.query);history.replaceState(null,'',url);
  if(state.cache.has(key)&&!force) {applyResult(state.cache.get(key),append,true);busy(false);return;}
  busy(true);
  try {
    const start=performance.now();
    const prepared=initial?.key===key;
    const data=prepared?initial.data:await api('/api/search',req,state.controller.signal);
    if(seq!==state.sequence)return;
    if(append && data.snapshotId!==state.result.snapshotId){catalogChanged();return;}
    if(!append){
      if(data.snapshotId!==state.meta.snapshotId){state.cache.clear();state.details.clear();}
      state.meta.snapshotId=data.snapshotId;state.snapshotMismatch=false;
    }
    if(!prepared)data.clientMs=Math.round(performance.now()-start);
    state.cache.set(key,data);if(state.cache.size>50)state.cache.delete(state.cache.keys().next().value);
    applyResult(data,append,false);
  } catch(e) {if(e.name!=='AbortError'&&seq===state.sequence){error(e.message);if(!state.result)$('#results').innerHTML='';}}
  finally {if(seq===state.sequence)busy(false);}
}

function applyResult(data,append=false,localCache=false) {
  state.result=data;state.query=data.request.query;
  if(!append){
    state.rows=[];state.contexts.clear();$('#results').innerHTML='';state.shareSearchId=data.searchId;
    const label=data.request.offset===0?'검색 첫 페이지 공유':`검색 시작 페이지 공유 (${data.request.offset+1}위부터)`;
    $('#share-button').title=label;$('#share-button').setAttribute('aria-label',label);
  }
  const start=state.rows.length;
  for(const p of data.hits){state.contexts.set(String(p.productId),{product:p,searchId:data.searchId,snapshotId:data.snapshotId});state.rows.push(p);}
  $('#results').insertAdjacentHTML('beforeend',data.hits.map((p,i)=>card(p,start+i)).join(''));
  $('#empty-state').hidden=state.rows.length>0;
  const pageEnded=data.candidateCount>0&&state.rows.length===0;
  $('#empty-state h2').textContent=pageEnded?'이 페이지에는 상품이 없어요':'조건에 맞는 상품이 없어요';
  $('#empty-state p').textContent=pageEnded?'같은 조건으로 처음부터 다시 검색하세요.':'검색어를 바꾸거나 적용한 필터를 줄여 보세요.';
  $('#empty-reset').textContent=pageEnded?'처음부터 검색':'필터 초기화';
  $('#load-more').hidden=nextOffset(data)===null;
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
  const context=state.contexts.get(String(p.productId)),rating=state.ratings.get(context.searchId+':'+p.productId);
  const type={Voucher:'상품권·이용권',Pickup:'픽업'}[p.productType];
  const label=p.matchedFields?.length?p.matchedFields.slice(0,2).join(' · '):(state.query?'의미 유사 후보':p.categoryGroup);
  const picture=p.image?`<img src="${esc(p.image)}" data-fallback="${esc(p.imageFallback)}" alt="${esc(p.name)}" width="384" height="384" loading="${index<12?'eager':'lazy'}" decoding="async" ${index<2?'fetchpriority="high"':''}>`:'<span class="product-image-placeholder" role="img" aria-label="이미지 없음">이미지 없음</span>';
  return `<article class="product-card" data-id="${esc(p.productId)}"><button class="card-open" data-action="detail" aria-label="${esc(p.name)} 상세 보기"><div class="product-image"><span class="product-rank">${p.rank}</span>${picture}${type?`<span class="product-kind">${type}</span>`:''}</div><div class="product-info"><div class="product-brand">${esc(p.brand)}</div><h2 class="product-name">${esc(p.name)}</h2><p class="product-description">${esc(p.description || '상품 상세에서 정보를 확인하세요.')}</p><div class="product-price-row"><strong class="product-price">${money(p.price)}<small>원</small></strong><span class="product-category">${esc(p.category)}</span></div></div></button><div class="card-bottom"><span class="match-label">${esc(label)}</span><div class="feedback-actions"><button data-action="relevant" class="${rating==='relevant'?'rated':''}" title="관련 있는 상품" aria-label="${esc(p.name)} 관련 있음">✓</button><button data-action="irrelevant" class="${rating==='irrelevant'?'rated':''}" title="관련 없는 상품" aria-label="${esc(p.name)} 관련 없음">✕</button></div></div></article>`;
}

function categoryTree(groups,prefix=''){
  const action=prefix?'제외':'선택';
  return groups.map(group=>`<div class="category-group" data-${prefix}category-group="${esc(group.id)}"><div class="category-row"><label class="category-check"><input type="checkbox" data-${prefix}group="${esc(group.id)}" aria-label="${esc(group.name)} 전체 ${action}"></label><button type="button" data-${prefix}toggle-group="${esc(group.id)}" aria-expanded="false" aria-controls="${prefix}category-children-${esc(group.id)}" aria-label="${esc(group.name)} 하위 분류 ${prefix?'제외 ':''}목록"><span>${esc(group.name)}</span><span class="category-selection"></span><span class="category-count">${money(group.count)}</span><span class="category-chevron" aria-hidden="true">⌄</span></button></div><div id="${prefix}category-children-${esc(group.id)}" class="category-children" hidden>${group.children.map(c=>`<label data-${prefix}category-choice="${esc(c.id)}"><input type="checkbox" data-${prefix}category="${esc(c.id)}" aria-label="${esc(c.name)} ${action}"><span>${esc(c.name)}</span><span class="category-count">${money(c.count)}</span></label>`).join('')}</div></div>`).join('');
}
function syncCategoryTree(prefix='',field='categoryIds'){
  $$(`[data-${prefix}category]`).forEach(c=>c.checked=Boolean(categorySelected(parseCategoryId(c.dataset[prefix?'excludeCategory':'category']),field)));
  $$(`[data-${prefix}group]`).forEach(c=>{
    const id=parseCategoryId(c.dataset[prefix?'excludeGroup':'group']),children=categoryChildren(id);
    const n=children.filter(child=>categorySelected(child.id,field)).length;
    c.checked=state.filters[field].includes(id)||(children.length>0&&n===children.length);c.indeterminate=!c.checked&&n>0;
    const label=$(`[data-${prefix}toggle-group="${id}"] .category-selection`);
    if(label)label.textContent=c.checked?(prefix?'전체 제외':'전체 선택'):n?`${n}개 ${prefix?'제외':'선택'}`:'';
  });
}

function renderMetadata() {
  $('#catalog-count').textContent=`${money(state.meta.productCount)}개 상품`;
  const groups=state.meta.groups.map(group=>({...group,children:categoryChildren(group.id)}));
  $('#categories').innerHTML=categoryTree(groups);
  $('#exclude-categories').innerHTML=categoryTree(groups,'exclude-');
  $('#brands-list').innerHTML=state.meta.brands.map(b=>`<option value="${esc(b.name)}"></option>`).join('');
  const options=groups.map(group=>`<optgroup label="${esc(group.name)}"><option value="${esc(group.id)}">${esc(group.name)} 전체</option>${group.children.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</optgroup>`).join('');
  ['#prefer-category','#downrank-category'].forEach(id=>$(id).innerHTML='<option value="">선택해서 추가</option>'+options);
  $('#about-body').innerHTML=`<p>챗봇·프로파일러와 같은 검색 코어로 상품 후보를 확인하는 QA 화면입니다.</p>${[['상품',`${money(state.meta.productCount)}개`],['검색 방식','키워드 + 의미 검색 · RRF'],['임베딩',state.meta.model],['벡터',`${state.meta.dimensions}차원 · 입력 최대 ${state.meta.maxTokens}토큰`],['카탈로그',state.meta.source],['Backend export 시각',state.meta.exportGeneratedAt||'정보 없음'],['순위 설정',state.meta.algorithm]].map(([k,v])=>`<div class="about-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}<p>표시 가격은 수집 당시 기준이며 현재 판매 상태는 미확인입니다. 검색 순위와 점수는 상품의 적합성을 보장하지 않습니다.</p><a class="about-link" href="/docs" target="_blank" rel="noopener">검색 API 문서 ↗</a>`;
}

function syncFilters() {
  for(const [id,field] of [['#min-price','minPrice'],['#max-price','maxPrice']])if(document.activeElement!==$(id))$(id).value=state.filters[field]??'';
  $$('[data-price]').forEach(b=>{const selected=state.filters.maxPrice===Number(b.dataset.price)&&state.filters.minPrice===null;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});
  syncCategoryTree();syncCategoryTree('exclude-','excludeCategoryIds');
  const excluded=(state.meta.categories??[]).filter(c=>categorySelected(c.id,'excludeCategoryIds'));
  $('#exclude-selection-count').textContent=excluded.length?`${excluded.length}개 하위 분류 제외`:'선택 없음';
  $('#clear-exclude-categories').hidden=!state.filters.excludeCategoryIds.length;
  const overlap=state.filters.categoryIds.length?excluded.filter(c=>categorySelected(c.id)):[];
  $('#category-conflict').hidden=!overlap.length;
  $('#category-conflict').textContent=overlap.length?`포함한 분류 중 ${overlap.length}개 하위 분류에 제외 조건이 우선 적용됩니다.`:'';
  $$('[name="product-type"]').forEach(c=>c.checked=state.filters.productTypes.includes(c.value));
  if(document.activeElement!==$('#exclude-products'))$('#exclude-products').value=state.filters.excludeProductIds.join(', ');$('#availability').value=state.filters.availability;
  const chips=[];
  const chip=(label,field,value,pref=false)=>{chips.push({label,field,value,pref});};
  if(state.filters.minPrice!==null||state.filters.maxPrice!==null)chip(`${state.filters.minPrice!==null?money(state.filters.minPrice):'0'} ~ ${state.filters.maxPrice!==null?money(state.filters.maxPrice):'제한 없음'}원`,'price',null);
  state.filters.categoryIds.forEach(v=>chip(categoryName(v),'categoryIds',v));
  state.filters.brands.forEach(v=>chip(v,'brands',v));
  state.filters.excludeBrands.forEach(v=>chip(`${v} 제외`,'excludeBrands',v));
  state.filters.excludeCategoryIds.forEach(v=>chip(`${categoryName(v)} 제외`,'excludeCategoryIds',v));
  state.filters.productTypes.forEach(v=>chip({Shipping:'배송 상품',Voucher:'상품권·이용권',Pickup:'픽업'}[v],'productTypes',v));
  if(state.filters.excludeProductIds.length)chip(`상품 ${state.filters.excludeProductIds.length}개 제외`,'excludeProductIds',null);
  if(state.filters.availability!=='any')chip({available:'판매 가능',available_or_unknown:'판매 불가 제외',unavailable:'판매 불가',unknown:'판매 상태 미확인'}[state.filters.availability],'availability',null);
  state.preferences.preferredCategoryIds.forEach(v=>chip(`${categoryName(v)} 선호`,'preferredCategoryIds',v,true));
  state.preferences.downrankCategoryIds.forEach(v=>chip(`${categoryName(v)} 비선호`,'downrankCategoryIds',v,true));
  state.chips=chips;
  $('#active-filters').innerHTML=chips.map((c,i)=>`<button data-chip="${i}" aria-label="${esc(c.label)} 조건 제거">${esc(c.label)}<span>×</span></button>`).join('');
  $('#filter-count').textContent=chips.length;$('#filter-count').hidden=!chips.length;$('#mobile-filter-count').textContent=chips.length||'';
  for(const [id,field,pref] of [['brand-chips','brands'],['exclude-brand-chips','excludeBrands'],['exclude-category-chips','excludeCategoryIds'],['prefer-category-chips','preferredCategoryIds',true],['downrank-category-chips','downrankCategoryIds',true]]) {
    $(`#${id}`).innerHTML=(pref?state.preferences:state.filters)[field].map(v=>`<button data-remove-field="${field}" data-value="${esc(v)}" ${pref?'data-pref="true"':''} aria-label="${esc(field.includes('Category')?categoryName(v):v)} 선택 해제">${esc(field.includes('Category')?categoryName(v):v)}<span>×</span></button>`).join('');
  }
}
function removeFilter(field,value,pref=false){
  const target=pref?state.preferences:state.filters;
  if(field==='price'){target.minPrice=null;target.maxPrice=null;}
  else if(field==='availability')target.availability='any';
  else target[field]=value===null?[]:target[field].filter(x=>String(x)!==String(value));
  syncFilters();run();
}
function reset(){state.filters=defaults();state.preferences=prefDefaults();syncFilters();run();}
function addBrand(inputId,field){const input=$(inputId),value=input.value.trim();if(!value)return;const brand=state.meta.brands.find(b=>b.name.toLocaleLowerCase()===value.toLocaleLowerCase());if(!brand){toast('목록에 있는 브랜드를 선택해 주세요.');return;}if(!state.filters[field].includes(brand.name))state.filters[field].push(brand.name);input.value='';syncFilters();run();}
function filterCategories(prefix=''){
  const query=$(`#${prefix}category-search`).value.trim().toLocaleLowerCase();let matches=0;
  for(const group of state.meta.groups){
    const row=$(`[data-${prefix}category-group="${group.id}"]`),button=$(`[data-${prefix}toggle-group="${group.id}"]`),children=$(`#${prefix}category-children-${group.id}`);
    const groupMatch=group.name.toLocaleLowerCase().includes(query);let visible=0;
    for(const category of categoryChildren(group.id)){
      const show=!query||groupMatch||category.name.toLocaleLowerCase().includes(query);
      $(`[data-${prefix}category-choice="${category.id}"]`).hidden=!show;if(show)visible++;
    }
    row.hidden=!visible;matches+=visible;
    const expanded=query?visible>0:button.dataset.expanded==='true';
    children.hidden=!expanded;button.setAttribute('aria-expanded',String(expanded));
  }
  $(`#${prefix}category-empty`).hidden=matches>0;
}

async function detail(id) {
  const context=state.contexts.get(String(id));if(!context)return;
  state.detail=context;
  $('#detail-dialog').showModal();document.body.style.overflow='hidden';
  $('#detail-body').innerHTML='<div class="loading-line">상품 정보를 불러오는 중…</div>';
  try {
    const detailKey=`${context.snapshotId}:${id}`;
    let product=state.details.get(detailKey);
    if(!product){const data=await api('/api/products',{ids:[context.product.productId],snapshotId:context.snapshotId});product=data.products[0];if(!product)throw Error('상품 정보를 찾을 수 없습니다.');state.details.set(detailKey,product);}
    if(state.detail!==context)return;
    const p=product,r=context.product;
    const sourceUrl=safeUrl(p.productUrl),imageUrl=p.imageLarge||p.image;
    const attrs=Object.entries(p.attributes);
    const labels={size:'크기',specification:'주요 사양',volume:'용량·수량',material:'소재',color:'색상',kind:'종류',usage:'사용 방법',food_type:'식품 유형',composition:'구성',recommended_age:'권장 연령',ingredients:'성분',use_and_form:'용도·형태',functional_information:'기능 정보'};
    $('#detail-body').innerHTML=`<div class="detail-product-head">${imageUrl?`<img class="detail-image" src="${esc(imageUrl)}" data-fallback="${esc(p.imageFallback)}" alt="${esc(p.name)}" width="768" height="768">`:'<div class="detail-image product-image-placeholder" role="img" aria-label="이미지 없음">이미지 없음</div>'}<div><div class="product-brand">${esc(p.brand)}</div><h2>${esc(p.name)}</h2><div class="detail-price">${money(p.price)}<small> 원</small></div><p class="source-note">수집 당시 가격 · 현재 판매 상태 미확인</p></div></div><div class="detail-links">${sourceUrl!=='#'?`<a href="${sourceUrl}" target="_blank" rel="noopener noreferrer">원본 상품 페이지 ↗</a>`:'<span class="source-unavailable">원본 링크 없음</span>'}<button data-detail-action="copy-id">상품 ID 복사</button></div><section class="detail-section"><h3>상품 설명</h3><p class="detail-description">${esc(p.description)}</p></section><section class="detail-section"><h3>상품 정보</h3><table class="detail-attributes"><tbody><tr><th>카테고리</th><td>${esc(p.categoryGroup)} › ${esc(p.category)}</td></tr><tr><th>유형</th><td>${{Shipping:'배송 상품',Voucher:'상품권·이용권',Pickup:'픽업'}[p.productType]||'미확인'}</td></tr>${attrs.map(([k,v])=>`<tr><th>${esc(labels[k]||k.replace('source_',''))}</th><td>${esc(v)}</td></tr>`).join('')}</tbody></table>${!attrs.length?'<p class="field-help">추가 속성 자료가 없습니다. 상품명과 설명에서 확인할 수 있는 정보만 표시합니다.</p>':''}</section>${p.tags.length?`<section class="detail-section"><h3>검색 보조 태그</h3><div class="detail-tags">${p.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div></section>`:''}<details class="detail-section"><summary>검색 근거·점수</summary><div class="score-grid"><div class="score-cell"><span>통합 순위 점수</span><strong>${Number(r.score).toFixed(4)}</strong></div><div class="score-cell"><span>키워드 점수</span><strong>${r.lexicalScore.toFixed(2)}</strong><small>${r.lexicalRank?`${r.lexicalRank}위`:''}</small></div><div class="score-cell"><span>의미 유사도</span><strong>${r.denseScore===null?'—':r.denseScore.toFixed(3)}</strong><small>${r.denseRank?`${r.denseRank}위`:''}</small></div></div><p class="source-note">${esc((r.matchedFields||[]).join(' · ')||'단어가 직접 일치한 필드 없음')}<br>점수는 추천 확률이 아닙니다. 키워드·벡터 점수의 단위가 다릅니다.</p></details><details class="detail-section"><summary>실제 검색 문서</summary><p class="source-note">${money(p.tokenCount)}토큰${p.embeddingTruncated?' · 벡터 입력 1,024토큰에서 잘림 · 원문은 아래에 보존':' · 벡터 입력에 전체 포함'}</p><pre class="source-text">${esc(p.sourceText)}</pre></details><section class="detail-section"><h3>이 검색 결과는 어땠나요?</h3><div class="detail-feedback"><button data-detail-action="relevant">✓ 관련 있음</button><button data-detail-action="irrelevant">✕ 관련 없음</button><button data-detail-action="filter_violation">조건 위반 제보</button></div></section>`;
  }catch(e){if(state.detail===context)$('#detail-body').innerHTML=`<div class="error-banner">${esc(e.message)}</div>`;}
}
function safeUrl(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#';}catch{return '#';}}
function closeDetail(){$('#detail-dialog').close();document.body.style.overflow='';state.detail=null;}
async function rate(context,verdict,note='') {
  try {await api('/api/feedback',{searchId:context.searchId,productId:context.product?.productId??null,verdict,note});state.ratings.set(context.searchId+':'+context.product?.productId,verdict);const cardEl=$$('.product-card').find(c=>c.dataset.id===String(context.product?.productId));if(cardEl)$$('.feedback-actions button',cardEl).forEach(b=>b.classList.toggle('rated',b.dataset.action===verdict));toast('QA 의견을 저장했습니다.');return true;}catch(e){toast(e.message);return false;}
}
function openNote(context,verdict){state.note={context,verdict};$('#note-title').textContent=verdict==='missing'?'누락된 상품 제보':'검색 조건 위반 제보';$('#note-description').textContent=verdict==='missing'?'나와야 하는 상품명이나 기대한 결과를 남겨 주세요.':context.product.name;$('#note-text').value='';$('#note-dialog').showModal();$('#note-text').focus();}
async function copy(text){try{await navigator.clipboard.writeText(text);toast('복사했습니다.');}catch{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();document.execCommand('copy');input.remove();toast('복사했습니다.');}}
const mobileFilters=typeof matchMedia==='function'?matchMedia('(max-width: 760px)'):null;
function setFilterAccess(){
  const mobile=Boolean(mobileFilters?.matches),panel=$('#filters-panel');
  panel.inert=mobile&&!filtersOpen;
  panel.setAttribute('role',mobile&&filtersOpen?'dialog':'complementary');
  if(mobile&&filtersOpen)panel.setAttribute('aria-modal','true');else panel.removeAttribute('aria-modal');
  panel.setAttribute('aria-labelledby','filters-title');
  for(const selector of ['.topbar','.search-band','#main-content'])$(selector).inert=mobile&&filtersOpen;
}
function openFilters(open){
  filtersOpen=open;$('#filters-panel').classList.toggle('open',open);$('#filter-scrim').hidden=!open;
  $('#open-filters').setAttribute('aria-expanded',String(open));document.body.style.overflow=open?'hidden':'';
  setFilterAccess();if(open)$('#close-filters').focus();else if(mobileFilters?.matches)$('#open-filters').focus();
}
$('#filters-panel').addEventListener('keydown',e=>{
  if(!filtersOpen||e.key!=='Tab')return;
  const focusable=$$('button,input,select,textarea,summary',$('#filters-panel')).filter(el=>!el.disabled&&el.getClientRects().length>0);
  const first=focusable[0],last=focusable.at(-1);
  if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
});
mobileFilters?.addEventListener('change',()=>{openFilters(false);setFilterAccess();});


$('#search-form').addEventListener('submit',e=>{e.preventDefault();run();$('#query').blur();});
$('#query').addEventListener('compositionstart',()=>state.composing=true);
$('#query').addEventListener('compositionend',()=>{state.composing=false;schedule(350);});
$('#query').addEventListener('input',()=>{if(!state.composing)schedule(350);});
$$('[data-query]').forEach(b=>b.addEventListener('click',()=>{$('#query').value=b.dataset.query;run();}));
document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&!$('dialog[open]')){e.preventDefault();$('#query').focus();}if(e.key==='Escape'&&filtersOpen)openFilters(false);});
$('#min-price').addEventListener('input',e=>{state.filters.minPrice=e.target.value===''?null:Number(e.target.value);schedule(450);});
$('#max-price').addEventListener('input',e=>{state.filters.maxPrice=e.target.value===''?null:Number(e.target.value);schedule(450);});
$$('[data-price]').forEach(b=>b.addEventListener('click',()=>{state.filters.minPrice=null;state.filters.maxPrice=Number(b.dataset.price);syncFilters();run();}));
for(const [prefix,field] of [['','categoryIds'],['exclude-','excludeCategoryIds']]){
  $(`#${prefix}categories`).addEventListener('change',e=>{
    const leaf=e.target.dataset[prefix?'excludeCategory':'category'],group=e.target.dataset[prefix?'excludeGroup':'group'];
    if(leaf)selectCategory(parseCategoryId(leaf),e.target.checked,false,field);
    else if(group)selectCategory(parseCategoryId(group),e.target.checked,true,field);
    else return;
    syncFilters();run();
  });
  $(`#${prefix}categories`).addEventListener('click',e=>{
    const button=e.target.closest(`[data-${prefix}toggle-group]`);if(!button)return;
    button.dataset.expanded=String(button.getAttribute('aria-expanded')!=='true');
    const expanded=button.dataset.expanded==='true';button.setAttribute('aria-expanded',String(expanded));
    $(`#${prefix}category-children-${button.getAttribute(`data-${prefix}toggle-group`)}`).hidden=!expanded;
  });
  $(`#${prefix}category-search`).addEventListener('input',()=>filterCategories(prefix));
}
$('#clear-exclude-categories').addEventListener('click',()=>removeFilter('excludeCategoryIds',null));
$$('[name="product-type"]').forEach(c=>c.addEventListener('change',()=>{state.filters.productTypes=$$('[name="product-type"]:checked').map(c=>c.value);run();}));
$('#add-brand').addEventListener('click',()=>addBrand('#brand-input','brands'));
$('#add-exclude-brand').addEventListener('click',()=>addBrand('#exclude-brand','excludeBrands'));
for(const [input,field] of [['#brand-input','brands'],['#exclude-brand','excludeBrands']]){$(input).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();addBrand(input,field);}});$(input).addEventListener('change',()=>addBrand(input,field));}
for(const [id,field,pref] of [['#prefer-category','preferredCategoryIds',true],['#downrank-category','downrankCategoryIds',true]]){$(id).addEventListener('change',e=>{if(!e.target.value)return;const target=pref?state.preferences:state.filters,value=parseCategoryId(e.target.value);if(!target[field].includes(value))target[field].push(value);e.target.value='';syncFilters();run();});}
$('#exclude-products').addEventListener('change',e=>{try{state.filters.excludeProductIds=[...new Set(e.target.value.split(/[,\s]+/).filter(Boolean))].map(parseProductId);run();}catch(e){error(e.message);}});
$('#availability').addEventListener('change',e=>{state.filters.availability=e.target.value;run();});
$('#active-filters').addEventListener('click',e=>{const b=e.target.closest('[data-chip]');if(b){const c=state.chips[Number(b.dataset.chip)];removeFilter(c.field,c.value,c.pref);}});
$('#filters-panel').addEventListener('click',e=>{const b=e.target.closest('[data-remove-field]');if(b)removeFilter(b.dataset.removeField,b.dataset.value,Boolean(b.dataset.pref));});
$('#reset-filters').addEventListener('click',reset);$('#empty-reset').addEventListener('click',()=>state.result?.candidateCount>0?run({force:true}):reset());
$$('[data-mode]').forEach(b=>b.addEventListener('click',()=>{state.mode=b.dataset.mode;$$('[data-mode]').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-pressed',String(x===b));});run();}));
for(const [id,list] of [['#grid-view',false],['#list-view',true]]){$(id).addEventListener('click',()=>{$('#results').classList.toggle('list-layout',list);$('#grid-view').classList.toggle('active',!list);$('#list-view').classList.toggle('active',list);$('#grid-view').setAttribute('aria-pressed',String(!list));$('#list-view').setAttribute('aria-pressed',String(list));});}
$('#load-more').addEventListener('click',()=>run({append:true}));
$('#results').addEventListener('click',e=>{const b=e.target.closest('[data-action]'),cardEl=e.target.closest('[data-id]');if(!b||!cardEl)return;const context=state.contexts.get(cardEl.dataset.id);if(b.dataset.action==='detail')detail(cardEl.dataset.id);else rate(context,b.dataset.action);});
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&e.target.dataset.fallback){const img=e.target;const fallback=img.dataset.fallback;delete img.dataset.fallback;img.src=fallback;}},true);
$('#close-detail').addEventListener('click',closeDetail);
$('#detail-dialog').addEventListener('cancel',()=>{document.body.style.overflow='';state.detail=null;});
$('#detail-dialog').addEventListener('click',e=>{if(e.target===$('#detail-dialog')&&e.clientX<$('#detail-dialog').getBoundingClientRect().left)closeDetail();});
$('#detail-body').addEventListener('click',e=>{const action=e.target.closest('[data-detail-action]')?.dataset.detailAction;if(!action||!state.detail)return;if(action==='copy-id')copy(String(state.detail.product.productId));else if(action==='filter_violation')openNote(state.detail,action);else rate(state.detail,action);});
$('#detail-exclude').addEventListener('click',()=>{if(!state.detail)return;state.filters.excludeProductIds=[...new Set([...state.filters.excludeProductIds,state.detail.product.productId])];closeDetail();syncFilters();run();});
$('#missing-button').addEventListener('click',()=>{if(state.result)openNote({searchId:state.result.searchId},'missing');});
$('#close-note').addEventListener('click',()=>$('#note-dialog').close());$('#cancel-note').addEventListener('click',()=>$('#note-dialog').close());
$('#note-form').addEventListener('submit',async e=>{e.preventDefault();const button=$('[type=submit]',$('#note-form'));button.disabled=true;try{if(await rate(state.note.context,state.note.verdict,$('#note-text').value.trim()))$('#note-dialog').close();}finally{button.disabled=false;}});
$('#share-button').addEventListener('click',()=>{if(state.shareSearchId)copy(`${location.origin}/?run=${state.shareSearchId}`);});
$('#export-button').addEventListener('click',()=>{if(!state.result)return;const blob=new Blob([stringifyJSON(state.result,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`search-${state.result.searchId.slice(0,8)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});
$('#about-button').addEventListener('click',()=>$('#about-dialog').showModal());$('#close-about').addEventListener('click',()=>$('#about-dialog').close());
$('#open-filters').addEventListener('click',()=>openFilters(true));$('#close-filters').addEventListener('click',()=>openFilters(false));$('#filter-scrim').addEventListener('click',()=>openFilters(false));

async function init(){
  setFilterAccess();skeletons();
  const params=new URLSearchParams(location.search);
  if(params.has('q'))$('#query').value=params.get('q');
  const filterInputs=$$('input,button,select,textarea',$('#filters-panel'));
  filterInputs.forEach(el=>el.disabled=true);
  try {
    // Neither request depends on the other; avoid an extra network round trip.
    const firstRequest=request(),key=stringifyJSON(firstRequest),start=performance.now();
    const first=params.has('run')?api(`/api/searches/${encodeURIComponent(params.get('run'))}`)
      :api('/api/search',firstRequest).then(data=>{data.clientMs=Math.round(performance.now()-start);return data;});
    const [metadata,firstResult]=await Promise.allSettled([api('/api/metadata'),first]);
    if(metadata.status==='rejected')throw metadata.reason;
    state.meta=metadata.value;renderMetadata();
    filterInputs.forEach(el=>el.disabled=false);
    if(firstResult.status==='rejected')throw firstResult.reason;
    const result=firstResult.value;
    if(params.has('run')){
      $('#query').value=result.request.query;state.filters={...defaults(),...result.request.filters};state.preferences={...prefDefaults(),...result.request.preferences};state.mode=result.request.mode;
      $$('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===state.mode);b.setAttribute('aria-pressed',String(b.dataset.mode===state.mode));});
      syncFilters();applyResult(result);busy(false);
      if(result.snapshotId!==state.meta.snapshotId)catalogChanged();
    }else{await run({initial:{key,data:result}});}
  }catch(e){error(e.message);$('#results').innerHTML='';busy(false);}
}
init();
