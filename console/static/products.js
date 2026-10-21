import {redact} from './core.js';

const MAX_CARDS = 3;
const validId = value => Number.isSafeInteger(value) && value > 0;

export function productImagePath(value, id) {
  // Signed upstream URLs and arbitrary hosts never reach the browser.
  return typeof value === 'string' && value === `/api/products/${id}/image` ? value : '';
}

export function productMetadata(value, id) {
  if (!value || value.productId !== id || !validId(id) ||
      typeof value.productName !== 'string' || !value.productName.trim() ||
      typeof value.brandName !== 'string' ||
      typeof value.unitPrice !== 'number' || !Number.isFinite(value.unitPrice) || value.unitPrice < 0) {
    throw new Error('PRODUCT_METADATA_UNAVAILABLE');
  }
  return {
    productId: id,
    productName: value.productName.slice(0, 500),
    brandName: value.brandName.slice(0, 200),
    unitPrice: value.unitPrice,
    imageUrl: productImagePath(value.imageUrl, id),
  };
}

/** A screen keeps one loader so SSE updates reuse in-flight product lookups. */
export function createProductLoader(fetchProduct, now = Date.now) {
  const cache = new Map();
  return (id, {refresh = false} = {}) => {
    if (!validId(id)) return Promise.reject(new Error('PRODUCT_ID_INVALID'));
    const saved = cache.get(id);
    if (!refresh && saved && now() - saved.created < 300000) return saved.promise;
    if (cache.size >= 128 && !cache.has(id)) cache.delete(cache.keys().next().value);
    const entry = {created: now(), promise: null};
    entry.promise = Promise.resolve().then(() => fetchProduct(id))
      .then(value => productMetadata(value, id))
      .catch(error => {
        if (cache.get(id) === entry) cache.delete(id);
        throw error;
      });
    cache.set(id, entry);
    return entry.promise;
  };
}

/** Display public product metadata; reason visibility follows the current experiment. */
export function createProductCards(recommendations, {reveal = false, loadProduct} = {}) {
  const list = document.createElement('div');
  list.className = 'product-cards';
  const seen = new Set();
  const products = (Array.isArray(recommendations) ? recommendations : [])
    .filter(item => {
      if (!validId(item?.productId) || seen.has(item.productId)) return false;
      seen.add(item.productId);
      return true;
    }).slice(0, MAX_CARDS);

  for (const item of products) {
    const id = item.productId;
    const card = document.createElement('article');
    card.className = 'product-card';
    card.dataset.productId = String(id);
    const photo = document.createElement('div');
    photo.className = 'product-photo';
    const info = document.createElement('div');
    info.className = 'product-info';
    const name = document.createElement('strong');
    name.textContent = `추천 상품 #${id}`;
    const meta = document.createElement('p');
    meta.className = 'product-meta';
    const reason = document.createElement('p');
    reason.className = 'product-reason';
    reason.textContent = typeof item.reason === 'string'
      ? String(redact(item.reason, reveal, 'reason')).slice(0, 2000) : '추천 이유 없음';
    const status = document.createElement('p');
    status.className = 'product-status';
    status.setAttribute('role', 'status');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'secondary';
    retry.textContent = '상품 정보 다시 시도';
    retry.hidden = true;
    info.append(name, meta, reason, status, retry);
    card.append(photo, info);
    list.append(card);

    function placeholder(message) {
      const fallback = document.createElement('span');
      fallback.className = 'product-photo-placeholder';
      fallback.textContent = message;
      photo.replaceChildren(fallback);
    }
    async function load(refresh = false) {
      retry.hidden = true;
      status.textContent = '상품 정보를 불러오는 중입니다.';
      placeholder('사진 확인 중');
      try {
        const data = productMetadata(await loadProduct(id, {refresh}), id);
        name.textContent = data.productName;
        meta.textContent = [data.brandName, `${data.unitPrice.toLocaleString('ko-KR')}원`, `#${id}`].filter(Boolean).join(' · ');
        if (!data.imageUrl) {
          placeholder('등록된 상품 사진 없음');
          status.textContent = '상품 사진을 확인할 수 없습니다.';
          return;
        }
        const image = document.createElement('img');
        image.alt = data.productName;
        image.decoding = 'async';
        image.referrerPolicy = 'no-referrer';
        image.draggable = false;
        image.onload = () => { status.textContent = ''; };
        image.onerror = () => {
          placeholder('상품 사진을 불러오지 못했습니다.');
          status.textContent = '상품 정보는 확인됐지만 사진 응답을 확인하지 못했습니다.';
          retry.hidden = false;
        };
        status.textContent = '상품 사진을 불러오는 중입니다.';
        photo.replaceChildren(image);
        image.src = data.imageUrl;
      } catch {
        placeholder('상품 정보 확인 필요');
        status.textContent = '상품 정보를 불러오지 못했습니다.';
        retry.hidden = false;
      }
    }
    retry.onclick = event => { event.stopPropagation(); void load(true); };
    void load();
  }
  return list;
}
