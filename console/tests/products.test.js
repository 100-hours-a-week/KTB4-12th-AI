import test from 'node:test';
import assert from 'node:assert/strict';
import {createProductCards, createProductLoader, productImagePath, productMetadata} from '../static/products.js';

const metadata = (id = 42) => ({productId:id, productName:'공개 상품', brandName:'브랜드', unitPrice:9900, imageUrl:`/api/products/${id}/image`});
const settle = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.attributes={}; this.textContent=''; this.hidden=false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  setAttribute(key, value) { this.attributes[key]=value; }
}
globalThis.document = {createElement: tag => new Element(tag)};
const descendants = element => [element, ...element.children.flatMap(descendants)];
const find = (root, className) => descendants(root).find(node => node.className === className);

test('only the exact same-origin image endpoint is accepted', () => {
  assert.equal(productImagePath('/api/products/42/image',42), '/api/products/42/image');
  for (const value of ['https://external.test/image', '//external.test/a', '/api/products/43/image', '/api/products/42/image?secret=x', 'javascript:alert(1)', 'data:image/svg+xml,<svg/>']) {
    assert.equal(productImagePath(value,42),'');
  }
  assert.throws(()=>productMetadata(metadata(99),42));
  assert.throws(()=>productMetadata({...metadata(),unitPrice:NaN},42));
});

test('one lookup is shared across SSE rerenders and is refreshed after five minutes', async () => {
  let calls=0, clock=0;
  const load = createProductLoader(async id => { calls++; return metadata(id); }, () => clock);
  const first=load(42), second=load(42);
  assert.equal(first,second);
  await Promise.all([first,second]);
  await load(42);
  assert.equal(calls,1);
  clock=300000;
  await load(42);
  assert.equal(calls,2);
  await load(42,{refresh:true});
  assert.equal(calls,3);
});

test('a failed lookup is retryable and never cached as product information', async () => {
  let calls=0;
  const load=createProductLoader(async id => { if(++calls===1)throw new Error('private upstream detail'); return metadata(id); });
  await assert.rejects(load(42));
  assert.equal((await load(42)).productId,42);
  assert.equal(calls,2);
});

test('cards use exact IDs, cap at three, and hide reasons until requested', async () => {
  const requested=[];
  const cards=createProductCards([{productId:42,reason:'본인 합성 이유'}, {productId:42,reason:'duplicate'}, {productId:0}, ...[43,44,45].map(productId=>({productId,reason:'합성'}))], {
    loadProduct: async id => {requested.push(id); return metadata(id);},
  });
  await settle();
  assert.deepEqual(requested,[42,43,44]);
  assert.equal(cards.children.length,3);
  assert.equal(find(cards,'product-reason').textContent,'[원문 숨김]');
  const image=descendants(cards).find(node=>node.tagName==='img');
  assert.equal(image.src,'/api/products/42/image');
  assert.equal(image.referrerPolicy,'no-referrer');
  image.onload();
  assert.equal(find(cards,'product-status').textContent,'');
});

test('visible experiment reasons still mask secrets and never become HTML', async () => {
  const text='<img onerror=alert(1)> Bearer synthetic-long-secret';
  const cards=createProductCards([{productId:42,reason:text}], {reveal:true,loadProduct:async()=>metadata()});
  await settle();
  const reason=find(cards,'product-reason');
  assert.equal(reason.textContent,'<img onerror=alert(1)> [토큰 마스킹]');
  assert.equal(reason.children.length,0);
});

test('failed image retains product and reason, supplies an honest placeholder, and supports retry', async () => {
  const refresh=[];
  const cards=createProductCards([{productId:42,reason:'합성 추천 이유'}], {reveal:true,loadProduct:async(id,opts)=>{refresh.push(opts.refresh);return metadata(id);}});
  await settle();
  descendants(cards).find(node=>node.tagName==='img').onerror();
  assert.equal(find(cards,'product-reason').textContent,'합성 추천 이유');
  assert.equal(find(cards,'product-photo-placeholder').textContent,'상품 사진을 불러오지 못했습니다.');
  const retry=descendants(cards).find(node=>node.tagName==='button');
  assert.equal(retry.hidden,false);
  let stopped=false;
  retry.onclick({stopPropagation(){stopped=true;}});
  await settle();
  assert.equal(stopped,true);
  assert.deepEqual(refresh,[false,true]);
});

test('lookup failures show only a safe diagnostic and can be retried', async () => {
  const cards=createProductCards([{productId:42,reason:'합성'}], {loadProduct:async()=>{throw new Error('private-signed-url');}});
  await settle();
  assert.equal(find(cards,'product-status').textContent,'상품 정보를 불러오지 못했습니다.');
  assert.equal(descendants(cards).some(node=>node.textContent.includes('private-signed-url')),false);
  assert.equal(descendants(cards).find(node=>node.tagName==='button').hidden,false);
});
