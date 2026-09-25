import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {parseJSON, stringifyJSON, parseProductId, parseCategoryId} from '../static/json.js';

// Run the production UI with a minimal DOM and deterministic HTTP responses.
function harness(snapshot='current') {
  const nodes=new Map(),lists=new Map();
  function node(selector) {
    if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,
      dataset:{},handlers:new Map(),classList:{toggle(){}},addEventListener(event,handler){this.handlers.set(event,handler);},attributes:{},setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k]??null;},removeAttribute(k){delete this.attributes[k];},focus(){},insertAdjacentHTML(){},
      querySelector:node,querySelectorAll:selector=>lists.get(selector)??[],showModal(){},close(){}});
    return nodes.get(selector);
  }
  const requests=[], responses=[], copied=[];
  const context=vm.createContext({document:{querySelector:node,querySelectorAll:selector=>lists.get(selector)??[],addEventListener(){},body:{style:{}}},
    location:new URL('http://localhost/'),navigator:{clipboard:{writeText:async text=>copied.push(text)}},history:{replaceState(){}},URL,URLSearchParams,AbortController,structuredClone,performance,setTimeout:()=>0,clearTimeout,
    parseJSON,stringifyJSON,parseProductId,parseCategoryId,fetch:async(url,init)=>{requests.push({url,body:init?.body?parseJSON(init.body):undefined});const reply=await responses.shift();return reply?.httpError?{ok:false,status:reply.status,text:async()=>stringifyJSON(reply.body)}:{ok:true,status:200,text:async()=>stringifyJSON(reply)};}});
  const source=readFileSync(new URL('../static/app.js',import.meta.url),'utf8').replace(/^import .*;\n/, '').replace(/init\(\);\s*$/, '');
  vm.runInContext(source,context);
  vm.runInContext(`state.meta={snapshotId:${JSON.stringify(snapshot)}};`,context);
  return {node,lists,requests,responses,copied,context,exec:code=>vm.runInContext(code,context),
    apply(result){context.fixture=result;vm.runInContext('applyResult(fixture)',context);}};
}
function page(offset=0,snapshot='current') {
  return {searchId:`${snapshot}-${offset}`,snapshotId:snapshot,
    request:{query:'',filters:{},preferences:{},mode:'hybrid',limit:48,offset},
    hits:Array.from({length:48},(_,i)=>({productId:offset+i+1,rank:offset+i+1,price:1000,score:0,lexicalScore:0,denseScore:null,matchedFields:[]})),
    candidateCount:150,eligibleCount:150,hasMore:true,nextOffset:offset+48,timing:{totalMs:1}};
}

test('a legacy shared page continues from its actual offset',async()=>{
  const ui=harness();const legacy=page(48);delete legacy.nextOffset;ui.apply(legacy);ui.responses.push(page(96));
  await ui.exec('run({append:true})');
  assert.equal(ui.requests[0].body.offset,96);
  assert.equal(ui.exec('state.rows.length'),96);
  assert.equal(ui.exec('new Set(state.rows.map(p=>p.productId)).size'),96);
});

test('a shared outdated snapshot cannot append',async()=>{
  const ui=harness('new');ui.apply(page(0,'old'));
  await ui.exec('run({append:true})');
  assert.equal(ui.requests.length,0);
  assert.equal(ui.exec('state.rows.length'),48);
  assert.equal(ui.node('#load-more').hidden,true);
  assert.match(ui.node('#error').textContent,/처음부터 다시 검색/);
});

test('a catalog change during pagination preserves the old page and requires a fresh search',async()=>{
  const ui=harness('old');ui.apply(page(0,'old'));ui.responses.push(page(48,'new'));
  await ui.exec('run({append:true})');
  assert.equal(ui.exec('state.rows.length'),48);
  assert.equal(ui.exec('state.result.snapshotId'),'old');
  assert.equal(ui.exec('state.snapshotMismatch'),true);
  assert.equal(ui.node('#load-more').hidden,true);
  ui.responses.push(page(0,'new'));await ui.exec('run()');
  assert.equal(ui.requests[1].body.offset,0);
  assert.equal(ui.exec('state.result.snapshotId'),'new');
  assert.equal(ui.exec('state.meta.snapshotId'),'new');
  assert.equal(ui.exec('state.snapshotMismatch'),false);
  assert.equal(ui.exec('state.rows.length'),48);
});

test('product details use the snapshot as part of their cache key',async()=>{
  const ui=harness();ui.apply(page());
  const detail={productId:1,name:'상품',price:1000,attributes:{},tags:[],tokenCount:10};
  ui.responses.push({products:[detail]});await ui.exec('detail(1)');
  ui.apply(page(0,'next'));ui.responses.push({products:[{...detail,name:'변경된 상품'}]});
  await ui.exec('detail(1)');
  assert.equal(ui.requests.length,2);
  assert.equal(ui.requests[1].body.snapshotId,'next');
  assert.equal(ui.exec("state.details.get('next:1').name"),'변경된 상품');
});


test('BIGINT values round-trip without changing their numeric JSON representation',()=>{
  const text='{"productId":9223372036854775807,"small":3670,"ids":[9007199254740993],"sourceProductId":"KAKAO_GIFT:7712218"}';
  const parsed=parseJSON(text);
  assert.equal(parsed.productId,9223372036854775807n);
  assert.equal(parsed.ids[0],9007199254740993n);
  assert.equal(parsed.small,3670);
  assert.equal(stringifyJSON(parsed),text);
  assert.equal(parseProductId('9223372036854775807'),9223372036854775807n);
  assert.throws(()=>parseProductId('9223372036854775808'));
  assert.throws(()=>parseProductId('1.5'));
  assert.throws(()=>parseProductId('0'));
});

test('lossless JSON keeps JSON syntax and string semantics',()=>{
  const samples=['null','true','false','-0','1.2e-3','[1, "\\n", null]', '{"__proto__":{"safe":true},"quote":"\\\\\\\""}'];
  for(const text of samples)assert.deepEqual(parseJSON(text),JSON.parse(text));
  for(const text of ['[1,]','{"x":}','01','true false','[','{"a" 1}'])assert.throws(()=>parseJSON(text),SyntaxError);
  assert.equal({}.safe,undefined);
});

test('detail requests retain large IDs from their DOM string keys',async()=>{
  const ui=harness();const result=page();result.hits[0].productId=9223372036854775807n;ui.apply(result);
  ui.responses.push({products:[{productId:9223372036854775807n,name:'상품',price:1000,attributes:{},tags:[],tokenCount:10}]});
  await ui.exec("detail('9223372036854775807')");
  assert.equal(ui.requests[0].body.ids[0],9223372036854775807n);
});


test('API console examples and detail lookup preserve BIGINT values',async()=>{
  const nodes=new Map(),handlers=new Map(),requests=[],rawRequests=[];let reply=null;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:'',addEventListener(event,handler){handlers.set(selector+':'+event,handler);},setAttribute(){}});return nodes.get(selector);};
  node('#request-source').value='chat';
  node('#request-body').value='{"query":"스피커", "filters":{"excludeProductIds":[9223372036854775807]}}';
  const context=vm.createContext({document:{querySelector:node,querySelectorAll:()=>[]},location:{origin:'http://localhost'},
    parseJSON,stringifyJSON,AbortSignal,performance,setTimeout,clearTimeout,
    fetch:async(path,options)=>{if(!options?.body)return {ok:true,text:async()=>'{"status":"ready","products":1}'};
      rawRequests.push(options.body);requests.push(parseJSON(options.body));if(reply)return reply;return {ok:true,status:200,text:async()=>' {"hits":[{"productId":9223372036854775807}],"snapshotId":"current"}'};}});
  const source=readFileSync(new URL('../static/guide.js',import.meta.url),'utf8').replace(/^import .*;\n/,'');
  vm.runInContext(source,context);
  for(const language of ['curl','python','javascript']){
    vm.runInContext(`language=${JSON.stringify(language)};renderExample();`,context);
    assert.match(node('#request-code').textContent,/9223372036854775807/);
    assert.doesNotMatch(node('#request-code').textContent,/9223372036854776000/);
  }
  await vm.runInContext("execute('/v1/search',payload())",context);
  assert.equal(requests[0].filters.excludeProductIds[0],9223372036854775807n);
  await vm.runInContext("execute('/v1/products',{ids:lastSearch.hits.map(hit=>hit.productId),snapshotId:lastSearch.snapshotId})",context);
  assert.equal(requests[1].ids[0],9223372036854775807n);
  // Preserve decimal/exponent tokens so the API's strict integer validation
  // rejects them instead of accidentally accepting a rounded integer.
  const raw='{"filters":{"excludeProductIds":[9.223372036854775807e18,3670.0]}}';
  node('#request-body').value=raw;
  for(const language of ['curl','python','javascript']){
    vm.runInContext(`language=${JSON.stringify(language)};renderExample();`,context);
    assert.match(node('#request-code').textContent,/9\.223372036854775807e18,3670\.0/);
  }
  handlers.get('#run-request:click')();
  assert.equal(rawRequests[2],raw);
  reply={ok:false,status:422,text:async()=>'{"message":"입력을 확인하세요","error":{"code":"INVALID_REQUEST","issues":[{"field":"limit","message":"1 이상"}]}}'};
  await vm.runInContext("execute('/v1/search',{})",context);
  assert.equal(node('#playground-error').hidden,false);
  assert.match(node('#playground-error').textContent,/INVALID_REQUEST.*limit: 1 이상/);
});


test('sharing after load more preserves the initial search page',async()=>{
  const ui=harness();ui.apply(page());ui.responses.push(page(48));await ui.exec('run({append:true})');
  assert.equal(ui.exec('state.rows.length'),96);
  ui.node('#share-button').handlers.get('click')();
  assert.equal(ui.copied[0],'http://localhost/?run=current-0');
  assert.equal(ui.node('#share-button').title,'검색 첫 페이지 공유');
  assert.equal(ui.exec('state.result.searchId'),'current-48');
});

test('resharing a restored later page preserves that starting page',async()=>{
  const ui=harness();ui.apply(page(48));ui.responses.push(page(96));await ui.exec('run({append:true})');
  ui.node('#share-button').handlers.get('click')();
  assert.equal(ui.copied[0],'http://localhost/?run=current-48');
  assert.equal(ui.node('#share-button').title,'검색 시작 페이지 공유 (49위부터)');
});


function categoryFixture(ui) {
  ui.context.categoryMetadata={snapshotId:'current',productCount:3,brands:[],
    groups:[{id:1,name:'생활',count:2},{id:9223372036854775806n,name:'기기',count:1}],
    categories:[{id:11,name:'컵',parentId:1,count:1},{id:12,name:'그릇',parentId:1,count:1},
      {id:9223372036854775807n,name:'스피커',parentId:9223372036854775806n,count:1}]};
  ui.exec('state.meta=categoryMetadata');
}

test('category metadata renders numeric parent and child options',()=>{
  const ui=harness();categoryFixture(ui);ui.exec('renderMetadata()');
  assert.match(ui.node('#categories').innerHTML,/data-group="1"/);
  assert.match(ui.node('#categories').innerHTML,/data-category="9223372036854775807"/);
  assert.match(ui.node('#prefer-category').innerHTML,/<option value="1">생활 전체<\/option>/);
  assert.equal(ui.exec('categoryName(1)'),'생활 전체');
  assert.equal(ui.exec('categoryName(11)'),'컵');
});

test('selecting a numeric category group checks children and sends only its parent ID',async()=>{
  const ui=harness();categoryFixture(ui);
  const group={dataset:{group:'1'}},leafA={dataset:{category:'11'}},leafB={dataset:{category:'12'}};
  ui.lists.set('[data-group]',[group]);ui.lists.set('[data-category]',[leafA,leafB]);
  ui.responses.push(page());ui.node('#categories').handlers.get('change')({target:{dataset:{group:'1'},checked:true}});
  await new Promise(setImmediate);
  assert.deepEqual(ui.requests[0].body.filters.categoryIds,[1]);
  assert.equal(group.checked,true);assert.equal(group.indeterminate,false);
  assert.equal(leafA.checked,true);assert.equal(leafB.checked,true);
  ui.responses.push(page());ui.node('#categories').handlers.get('change')({target:{dataset:{category:'11'},checked:false}});
  await new Promise(setImmediate);
  assert.deepEqual(ui.requests[1].body.filters.categoryIds,[12]);
  assert.equal(group.checked,false);assert.equal(group.indeterminate,true);
  assert.equal(leafA.checked,false);assert.equal(leafB.checked,true);
});

test('category exclusion and preferences keep BIGINT IDs and their removable chips',async()=>{
  const ui=harness();categoryFixture(ui);
  ui.responses.push(page());ui.node('#exclude-categories').handlers.get('change')({target:{dataset:{excludeGroup:'9223372036854775806'},checked:true}});
  await new Promise(setImmediate);
  for(const selector of ['#prefer-category','#downrank-category']){
    ui.responses.push(page());ui.node(selector).handlers.get('change')({target:{value:'9223372036854775806'}});
    await new Promise(setImmediate);
  }
  assert.equal(ui.requests[0].body.filters.excludeCategoryIds[0],9223372036854775806n);
  assert.equal(ui.requests[1].body.preferences.preferredCategoryIds[0],9223372036854775806n);
  assert.equal(ui.requests[2].body.preferences.downrankCategoryIds[0],9223372036854775806n);
  assert.match(ui.node('#exclude-category-chips').innerHTML,/기기 전체/);
  ui.responses.push(page());await ui.exec("removeFilter('excludeCategoryIds','9223372036854775806')");
  assert.equal(ui.exec('state.filters.excludeCategoryIds.length'),0);
  assert.equal(parseCategoryId('9223372036854775807'),9223372036854775807n);
  assert.throws(()=>parseCategoryId('CAT-01'));
});


test('products without source images or links show an explicit empty state',async()=>{
  const ui=harness();const result=page();result.hits[0]={...result.hits[0],image:'',imageFallback:'',productType:null,sourceProductId:null,sourceCategoryId:null};
  ui.apply(result);const card=ui.exec('card(state.rows[0],0)');
  assert.match(card,/이미지 없음/);assert.doesNotMatch(card,/<img[^>]*src=""/);
  const summary=result.hits[0];
  ui.responses.push({products:[{...summary,imageLarge:'',productUrl:'',name:'상품',description:'',attributes:{},tags:[],tokenCount:10}]});
  await ui.exec('detail(1)');
  assert.match(ui.node('#detail-body').innerHTML,/이미지 없음/);
  assert.match(ui.node('#detail-body').innerHTML,/원본 링크 없음/);
  assert.doesNotMatch(ui.node('#detail-body').innerHTML,/<a href="#"/);
});


test('the UI follows server nextOffset and treats null as pagination completion',async()=>{
  const ui=harness();const first=page();first.nextOffset=100;ui.apply(first);
  const last=page(100);last.nextOffset=null;last.hasMore=false;ui.responses.push(last);
  await ui.exec('run({append:true})');
  assert.equal(ui.requests[0].body.offset,100);
  assert.equal(ui.node('#load-more').hidden,true);
  await ui.exec('run({append:true})');
  assert.equal(ui.requests.length,1);
});

test('an exhausted page is distinguished from no candidates without clearing filters',async()=>{
  const ui=harness();const last=page(7000);last.hits=[];last.hasMore=false;last.nextOffset=null;last.status='OK';
  ui.apply(last);ui.exec('state.filters.maxPrice=30000');
  assert.equal(ui.node('#empty-state h2').textContent,'이 페이지에는 상품이 없어요');
  ui.responses.push(page());await ui.node('#empty-reset').handlers.get('click')();
  assert.equal(ui.requests[0].body.offset,0);
  assert.equal(ui.requests[0].body.filters.maxPrice,30000);
  const noMatch={...last,candidateCount:0,status:'NO_MATCH'};ui.apply(noMatch);
  assert.equal(ui.node('#empty-state h2').textContent,'조건에 맞는 상품이 없어요');
});

test('structured HTTP errors expose code, field issues and status',async()=>{
  const ui=harness();ui.responses.push({httpError:true,status:422,body:{message:'입력을 확인하세요',error:{code:'UNKNOWN_CATEGORY',issues:[{field:'filters.categoryIds',message:'존재하지 않는 분류'}]}}});
  await assert.rejects(ui.exec("api('/v1/search',{})"),error=>{
    assert.equal(error.code,'UNKNOWN_CATEGORY');assert.equal(error.status,422);
    assert.equal(error.issues[0].field,'filters.categoryIds');
    assert.match(error.message,/UNKNOWN_CATEGORY.*filters\.categoryIds.*존재하지 않는 분류/);return true;
  });
});

test('available_or_unknown is sent unchanged and labeled as excluding unavailable stock',async()=>{
  const ui=harness();ui.responses.push(page());
  ui.node('#availability').handlers.get('change')({target:{value:'available_or_unknown'}});
  await new Promise(setImmediate);
  assert.equal(ui.requests[0].body.filters.availability,'available_or_unknown');
  assert.match(ui.node('#active-filters').innerHTML,/판매 불가 제외/);
});

test('exclusion tree supports whole groups, partial undo and immediate search with cached reuse',async()=>{
  const ui=harness();categoryFixture(ui);
  const group={dataset:{excludeGroup:'1'}},a={dataset:{excludeCategory:'11'}},b={dataset:{excludeCategory:'12'}};
  ui.lists.set('[data-exclude-group]',[group]);ui.lists.set('[data-exclude-category]',[a,b]);
  const change=async(dataset,checked)=>{ui.responses.push(page());ui.node('#exclude-categories').handlers.get('change')({target:{dataset,checked}});await new Promise(setImmediate);};
  await change({excludeGroup:'1'},true);
  assert.equal(ui.requests.length,1);assert.deepEqual(ui.requests[0].body.filters.excludeCategoryIds,[1]);
  assert.equal(group.checked,true);assert.equal(a.checked,true);assert.equal(b.checked,true);
  await change({excludeCategory:'11'},false);
  assert.equal(ui.requests.length,2);assert.deepEqual(ui.requests[1].body.filters.excludeCategoryIds,[12]);
  assert.equal(group.indeterminate,true);assert.equal(a.checked,false);assert.equal(b.checked,true);
  await change({excludeCategory:'11'},true);
  // Restoring the last child canonicalizes to the parent without duplicate chips.
  assert.deepEqual(Array.from(ui.exec('request().filters.excludeCategoryIds')),[1]);
  assert.equal(ui.requests.length,2); // The original whole-group search is cached.
  assert.equal(group.indeterminate,false);
  ui.responses.push(page());ui.node('#clear-exclude-categories').handlers.get('click')();await new Promise(setImmediate);
  assert.equal(ui.exec('state.filters.excludeCategoryIds.length'),0);
  assert.equal(a.checked,false);assert.equal(b.checked,false);
});

test('finding categories does not search products or discard hidden exclusions',()=>{
  const ui=harness();categoryFixture(ui);
  ui.exec('state.filters.excludeCategoryIds=[12];syncFilters()');
  ui.node('#exclude-category-search').value='컵';ui.node('#exclude-category-search').handlers.get('input')();
  assert.equal(ui.requests.length,0);
  assert.equal(ui.node('[data-exclude-category-choice="11"]').hidden,false);
  assert.equal(ui.node('[data-exclude-category-choice="12"]').hidden,true);
  assert.equal(ui.node('#exclude-category-children-1').hidden,false);
  assert.equal(ui.node('[data-exclude-category-group="9223372036854775806"]').hidden,true);
  assert.equal(ui.exec('state.filters.excludeCategoryIds[0]'),12);
  ui.node('#exclude-category-search').value='존재하지않음';ui.node('#exclude-category-search').handlers.get('input')();
  assert.equal(ui.node('#exclude-category-empty').hidden,false);
  ui.node('#exclude-category-search').value='';ui.node('#exclude-category-search').handlers.get('input')();
  assert.equal(ui.node('[data-exclude-category-choice="12"]').hidden,false);
  assert.equal(ui.node('#exclude-category-empty').hidden,true);
});

test('inclusion and exclusion overlap is explained and clearing exclusions preserves other filters',async()=>{
  const ui=harness();categoryFixture(ui);
  ui.exec('state.filters.categoryIds=[1];state.filters.excludeCategoryIds=[11];state.filters.maxPrice=30000;syncFilters()');
  assert.equal(ui.node('#category-conflict').hidden,false);
  assert.match(ui.node('#category-conflict').textContent,/1개.*제외 조건이 우선/);
  ui.responses.push(page());ui.node('#clear-exclude-categories').handlers.get('click')();await new Promise(setImmediate);
  assert.deepEqual(ui.requests[0].body.filters.categoryIds,[1]);assert.equal(ui.requests[0].body.filters.maxPrice,30000);
  assert.equal(ui.node('#category-conflict').hidden,true);
});

test('expanding exclusion children is separate from checking and preserves focusable controls',()=>{
  const ui=harness();categoryFixture(ui);
  const button=ui.node('[data-exclude-toggle-group="1"]');button.setAttribute('data-exclude-toggle-group','1');button.setAttribute('aria-expanded','false');
  ui.node('#exclude-categories').handlers.get('click')({target:{closest:()=>button}});
  assert.equal(button.getAttribute('aria-expanded'),'true');assert.equal(ui.node('#exclude-category-children-1').hidden,false);
  assert.equal(ui.requests.length,0);assert.equal(ui.exec('state.filters.excludeCategoryIds.length'),0);
});


test('startup fetches metadata and search in parallel without a duplicate search',async()=>{
  const ui=harness();categoryFixture(ui);
  let release;ui.responses.push(new Promise(resolve=>release=resolve),ui.exec('state.meta'));
  const pending=ui.exec('init()');
  await new Promise(setImmediate);
  assert.deepEqual(ui.requests.map(r=>r.url),['/api/search','/api/metadata']);
  release(page());await pending;
  assert.equal(ui.requests.length,2);
  assert.equal(ui.exec('state.rows.length'),48);
});

test('typing during initial load cannot replace a newer query with old prefetched results',async()=>{
  const ui=harness();categoryFixture(ui);
  let release;ui.responses.push(new Promise(resolve=>release=resolve),ui.exec('state.meta'));
  const pending=ui.exec('init()');
  ui.node('#query').value='새 검색';
  const newer=page();newer.request.query='새 검색';ui.responses.push(newer);
  release(page());await pending;
  assert.equal(ui.requests.length,3);
  assert.equal(ui.requests[2].body.query,'새 검색');
  assert.equal(ui.exec('state.query'),'새 검색');
});

test('a failed initial search retains metadata so the user can retry',async()=>{
  const ui=harness();categoryFixture(ui);
  ui.responses.push({httpError:true,status:503,body:{message:'Busy'}},ui.exec('state.meta'));
  await ui.exec('init()');
  assert.equal(ui.node('#error').textContent,'Busy');
  ui.responses.push(page());await ui.exec('run()');
  assert.equal(ui.exec('state.rows.length'),48);
});
