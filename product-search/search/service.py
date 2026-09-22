"""Framework-free shared retrieval. All callers use identical validation and ranking."""
import asyncio
from collections import Counter, OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import unicodedata
import numpy as np
from .models import SearchRequest

ALGORITHM = "bm25f-4-3-2-1-05+jina768-exact+rrf30/1"


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def tokens(text):
    words = re.findall(r"[^\W_]+", normalize(text), flags=re.UNICODE)
    return words + [w[i:i+2] for w in words if len(w) > 2 for i in range(len(w)-1)]


class SearchService:
    def __init__(self, data_dir: Path, embedding=None):
        self.data_dir = Path(data_dir)
        raw = (self.data_dir / "catalog.json").read_bytes()
        self.catalog = json.loads(raw)
        self.manifest = json.loads((self.data_dir / "manifest.json").read_text())
        vectors_raw = (self.data_dir / "vectors.f32").read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.manifest['catalog_sha256'] or hashlib.sha256(vectors_raw).hexdigest() != self.manifest['vectors_sha256']:
            raise ValueError("catalog_embedding_hash_mismatch")
        self.products = self.catalog['products']
        ids = [p['id'] for p in self.products]
        if ids != self.manifest['product_ids'] or len(set(ids)) != len(ids):
            raise ValueError("catalog_embedding_id_mismatch")
        self.vectors = np.frombuffer(vectors_raw, dtype=np.float32).reshape(len(ids), 768).copy()
        if not np.isfinite(self.vectors).all() or np.any(np.linalg.norm(self.vectors, axis=1) == 0):
            raise ValueError("invalid_catalog_vectors")
        self.vectors /= np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.vectors.flags.writeable = False
        self.by_id = {p['id']: i for i, p in enumerate(self.products)}
        self.snapshot_id = self.manifest['catalog_sha256'][:20]
        self.embedding = embedding
        self.prices = np.asarray([p['price'] for p in self.products])
        self.categories = np.asarray([p['category_id'] for p in self.products])
        self.brands = np.asarray([normalize(p['brand']) for p in self.products])
        self.types = np.asarray([p['product_type'] for p in self.products])
        self.availability = np.asarray([p['availability'] for p in self.products])
        self.category_ids = set(self.categories.tolist()) | {c['category_id'] for c in self.catalog['taxonomy']['categories']}
        self.brand_keys = set(self.brands.tolist())
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieval")
        self.cache = OrderedDict()
        self._build_lexical()
        groups = defaultdict(deque)
        for i, p in enumerate(self.products):
            groups[p['category_id']].append(i)
        self.browse_order = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    self.browse_order.append(group.popleft())

    def _fields(self, p):
        return [p['name'], p['brand'], ' '.join([p['category_group'], p['category'], p['kind']]),
                p['description'] + ' ' + ' '.join(f'{k} {v}' for k, v in p['attributes'].items()), ' '.join(p['tags'])]

    def _build_lexical(self):
        fields = [[Counter(tokens(text)) for text in self._fields(p)] for p in self.products]
        lengths = np.asarray([[sum(f.values()) for f in p] for p in fields], dtype=np.float32)
        averages = np.maximum(lengths.mean(axis=0), 1)
        postings = defaultdict(dict)
        for i, product_fields in enumerate(fields):
            for j, (field, weight) in enumerate(zip(product_fields, [4., 3., 2., 1., .5])):
                norm = .25 + .75 * lengths[i,j] / averages[j]
                for term, count in field.items():
                    postings[term][i] = postings[term].get(i, 0.) + weight * count / norm
        n = len(self.products)
        self.postings = {}
        for term, docs in postings.items():
            idx = np.fromiter(docs.keys(), dtype=np.int32)
            tf = np.fromiter(docs.values(), dtype=np.float32)
            idf = math.log(1 + (n-len(docs)+.5)/(len(docs)+.5))
            self.postings[term] = (idx, (idf * tf * 2.2 / (tf + 1.2)).astype(np.float32))

    def _validate(self, request):
        f, p = request.filters, request.preferences
        unknown = set(f.category_ids + f.exclude_category_ids + p.preferred_category_ids + p.downrank_category_ids) - self.category_ids
        if unknown:
            raise ValueError("카테고리를 확인해 주세요: " + ', '.join(sorted(unknown)))
        unknown_brands = {normalize(b) for b in f.brands + f.exclude_brands} - self.brand_keys
        if unknown_brands:
            raise ValueError("브랜드 목록에서 선택해 주세요: " + ', '.join(sorted(unknown_brands)))

    def _eligible(self, request):
        f = request.filters
        mask = np.ones(len(self.products), dtype=bool)
        if f.min_price is not None: mask &= self.prices >= f.min_price
        if f.max_price is not None: mask &= self.prices <= f.max_price
        if f.category_ids: mask &= np.isin(self.categories, f.category_ids)
        if f.exclude_category_ids: mask &= ~np.isin(self.categories, f.exclude_category_ids)
        if f.brands: mask &= np.isin(self.brands, [normalize(b) for b in f.brands])
        if f.exclude_brands: mask &= ~np.isin(self.brands, [normalize(b) for b in f.exclude_brands])
        if f.product_types: mask &= np.isin(self.types, f.product_types)
        if f.availability != 'any': mask &= self.availability == f.availability
        for id in f.exclude_product_ids:
            if id in self.by_id: mask[self.by_id[id]] = False
        return mask

    def _rank(self, request, mask, vector):
        started = time.perf_counter()
        lexical_scores = np.zeros(len(self.products), dtype=np.float32)
        dense_scores = np.zeros(len(self.products), dtype=np.float32)
        query = normalize(request.query)
        lexical, dense, fused = [], [], {}
        if query:
            if request.mode != 'dense':
                for token, count in Counter(tokens(query)).items():
                    posting = self.postings.get(token)
                    if posting is not None:
                        idx, weights = posting
                        lexical_scores[idx] += weights * min(count, 2)
                eligible = np.flatnonzero(mask & (lexical_scores > 0))
                lexical = sorted(eligible.tolist(), key=lambda i: (-float(lexical_scores[i]), self.products[i]['id']))[:100]
            if vector is not None:
                dense_scores = self.vectors @ vector
                dense = sorted(np.flatnonzero(mask).tolist(), key=lambda i: (-float(dense_scores[i]), self.products[i]['id']))[:100]
            for ranking in [lexical, dense]:
                for rank, i in enumerate(ranking, 1):
                    fused[i] = fused.get(i, 0.) + 1 / (30 + rank)
            for i in fused:
                category = self.products[i]['category_id']
                if category in request.preferences.preferred_category_ids: fused[i] *= 1.10
                if category in request.preferences.downrank_category_ids: fused[i] *= .75
            order = sorted(fused, key=lambda i: (-fused[i], -float(lexical_scores[i]), self.products[i]['id']))
        else:
            order = [i for i in self.browse_order if mask[i]]
            # Explicit preferences apply to browsing too, with stable order as the tie breaker.
            order.sort(key=lambda i: (self.products[i]['category_id'] in request.preferences.downrank_category_ids,
                                     -(self.products[i]['category_id'] in request.preferences.preferred_category_ids)))
        lex_ranks = {i:r for r,i in enumerate(lexical,1)}
        den_ranks = {i:r for r,i in enumerate(dense,1)}
        fields_labels = ['상품명','브랜드','분류·종류','설명·속성','태그']
        qt = set(re.findall(r"[^\W_]+", query))
        hits = []
        for rank, i in enumerate(order[request.offset:request.offset+request.limit], request.offset+1):
            assert mask[i], "hard_filter_violation"
            p = self.products[i]
            summary = self._summary(p)
            summary.update({'rank':rank,'score':round(fused.get(i,0.),7),
                'lexicalScore':round(float(lexical_scores[i]),4),'denseScore':round(float(dense_scores[i]),5) if vector is not None else None,
                'lexicalRank':lex_ranks.get(i),'denseRank':den_ranks.get(i),
                'matchedFields':[label for label,text in zip(fields_labels,self._fields(p)) if any(t in normalize(text) for t in qt)]})
            hits.append(summary)
        return {'hits':hits, 'eligibleCount':int(mask.sum()), 'candidateCount':len(order),
                'hasMore':request.offset+len(hits)<len(order), 'status':'OK' if hits else 'NO_MATCH',
                'retrievalMs':round((time.perf_counter()-started)*1000,2)}

    def _summary(self, p):
        return {'id':p['id'],'backendProductId':p['backend_product_id'],'name':p['name'],'brand':p['brand'],
                'categoryId':p['category_id'],'category':p['category'],'categoryGroup':p['category_group'],
                'price':p['price'],'description':p['description'][:300], 'productType':p['product_type'],
                'availability':p['availability'],'image':p['image'],'imageLarge':p['image_large'],'imageFallback':p['image_fallback']}

    def _check_snapshot(self, snapshot):
        if snapshot is not None and snapshot != self.snapshot_id:
            raise ValueError("카탈로그 버전이 변경되었습니다. 검색을 다시 실행해 주세요.")

    async def search(self, request: SearchRequest, snapshot=None, source='qa'):
        if not isinstance(request, SearchRequest):
            request = SearchRequest.model_validate(request)
        self._check_snapshot(snapshot)
        self._validate(request)
        start = time.perf_counter()
        key = request.model_dump_json()
        if key in self.cache:
            self.cache.move_to_end(key)
            result = copy.deepcopy(self.cache[key])
            result['timing'] = {'totalMs':round((time.perf_counter()-start)*1000,2),'embeddingMs':0,'retrievalMs':0,'cached':True,'embeddingCached':True}
            return result
        mask = self._eligible(request)
        embed_ms, cached, vector = 0., False, None
        if request.query and request.mode != 'lexical' and mask.any():
            if self.embedding is None: raise RuntimeError("임베딩 실행기가 준비되지 않았습니다.")
            t = time.perf_counter()
            vector, cached = await self.embedding.get(normalize(request.query), source)
            embed_ms = (time.perf_counter()-t)*1000
        result = await asyncio.get_running_loop().run_in_executor(self.pool, self._rank, request, mask, vector)
        result.update({'snapshotId':self.snapshot_id,'algorithm':ALGORITHM,'request':request.model_dump(by_alias=True),
                       'timing':{'totalMs':round((time.perf_counter()-start)*1000,2),'embeddingMs':round(embed_ms,2),
                                 'retrievalMs':result.pop('retrievalMs'),'cached':False,'embeddingCached':cached}})
        self.cache[key] = copy.deepcopy(result)
        if len(self.cache)>256: self.cache.popitem(last=False)
        return result

    def get_products(self, ids, snapshot=None):
        self._check_snapshot(snapshot)
        products, missing = [], []
        for id in ids:
            if id not in self.by_id:
                missing.append(id)
                continue
            i = self.by_id[id]; p = self.products[i]
            item = self._summary(p)
            item.update({'description':p['description'],'attributes':p['attributes'],'tags':p['tags'],
                'sourceText':p['document'],'productUrl':p['product_url'],'descriptionOrigin':p['description_origin'],
                'tokenCount':self.manifest['token_counts'][i], 'embeddingTruncated':self.manifest['token_counts'][i]>self.manifest['max_tokens']})
            products.append(item)
        return {'snapshotId':self.snapshot_id,'products':products,'missingIds':missing}

    def get_metadata(self):
        counts=Counter(self.categories)
        categories=[{'id':c['category_id'],'name':c['category'],'group':c['category_group'],'count':counts[c['category_id']]} for c in self.catalog['taxonomy']['categories']]
        brands=Counter(p['brand'] for p in self.products)
        return {'snapshotId':self.snapshot_id,'algorithm':ALGORITHM,'productCount':len(self.products),
            'categories':categories,'brands':[{'name':b,'count':n} for b,n in sorted(brands.items())],
            'productTypes':[{'id':t,'count':n} for t,n in Counter(self.types.tolist()).items()],
            'model':self.manifest['model'],'dimensions':768,'maxTokens':self.manifest['max_tokens'],
            'availabilityNote':self.catalog['availability_note'],'source':self.catalog['source']}

    def close(self):
        self.pool.shutdown(wait=True)
