"""Framework-free shared retrieval. All callers use identical validation and ranking."""
import asyncio
from collections import Counter, OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import time
import numpy as np
from .models import SearchRequest, ProductRequest, SnapshotMismatch, InvalidSearchFilter
from .version import snapshot_id
from .identity import product_id
from .tokenization import normalize, tokens, batch_tokens, TOKENIZER_ID, TOKENIZER_INFO

# Bump when ranking weights, candidate limits, tie-breaking or browse order change;
# this identifier participates in snapshotId and guards continuation requests.
ALGORITHM = f"bm25f-{TOKENIZER_ID}-4-3-2-1-05+jina768-exact+rrf30/2"


class SearchService:
    def __init__(self, data_dir: Path, embedding=None, *, encoder_fingerprint=None):
        self.data_dir = Path(data_dir)
        raw = (self.data_dir / "catalog.json").read_bytes()
        self.catalog = json.loads(raw)
        self.manifest = json.loads((self.data_dir / "manifest.json").read_text())
        vectors_raw = (self.data_dir / "vectors.f32").read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.manifest['catalog_sha256'] or hashlib.sha256(vectors_raw).hexdigest() != self.manifest['vectors_sha256']:
            raise ValueError("catalog_embedding_hash_mismatch")
        self.products = self.catalog['products']
        ids = [product_id(p['id']) for p in self.products]
        sources = [p['source_product_id'] for p in self.products]
        known_sources = [s for s in sources if s is not None]
        if any(not isinstance(s, str) or not s for s in known_sources) or len(set(known_sources)) != len(known_sources):
            raise ValueError('invalid_source_product_ids')
        if ids != self.manifest['product_ids'] or len(set(ids)) != len(ids):
            raise ValueError("catalog_embedding_id_mismatch")
        self.vectors = np.frombuffer(vectors_raw, dtype=np.float32).reshape(len(ids), 768).copy()
        if not np.isfinite(self.vectors).all() or np.any(np.linalg.norm(self.vectors, axis=1) == 0):
            raise ValueError("invalid_catalog_vectors")
        self.vectors /= np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.vectors.flags.writeable = False
        self.by_id = {p['id']: i for i, p in enumerate(self.products)}
        # Preserve the exact source-ID tie order, including stable catalog order.
        identity_order = sorted(range(len(self.products)), key=self._identity_sort_key)
        self.identity_ranks = np.empty(len(self.products), dtype=np.int64)
        self.identity_ranks[identity_order] = np.arange(len(self.products))
        effective_manifest = dict(self.manifest)
        if encoder_fingerprint is not None:
            effective_manifest['encoder_fingerprint'] = encoder_fingerprint
        self.snapshot_id = snapshot_id(self.catalog, effective_manifest, ALGORITHM)
        self.embedding = embedding
        self.prices = np.asarray([p['price'] for p in self.products])
        self._build_categories()
        self.categories = np.asarray([p['category_id'] for p in self.products], dtype=np.int64)
        self.brands = np.asarray([normalize(p['brand']) for p in self.products])
        self.types = np.asarray([p['product_type'] for p in self.products])
        self.availability = np.asarray([p['availability'] for p in self.products])
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

    def _build_categories(self):
        categories = self.catalog['taxonomy']['categories']
        ids = [product_id(c['category_id']) for c in categories]
        sources = [c['source_category_id'] for c in categories]
        known_sources = [s for s in sources if s is not None]
        if len(set(ids)) != len(ids) or any(not isinstance(s, str) or not s for s in known_sources) or len(set(known_sources)) != len(known_sources):
            raise ValueError('invalid_category_identity')
        self.taxonomy_by_id = {c['category_id']: c for c in categories}
        self.category_ids = set(ids)
        self.category_leaves = {id: set() for id in ids}
        for category in categories:
            id, parent = category['category_id'], category['parent_id']
            if parent is None:
                continue
            product_id(parent)
            if parent not in self.taxonomy_by_id or self.taxonomy_by_id[parent]['parent_id'] is not None:
                raise ValueError('invalid_category_parent')
            self.category_leaves[id].add(id)
            self.category_leaves[parent].add(id)
        for p in self.products:
            id = product_id(p['category_id'])
            parent = product_id(p['parent_category_id'])
            category = self.taxonomy_by_id.get(id)
            if category is None or category['parent_id'] != parent or category['source_category_id'] != p['source_category_id']:
                raise ValueError('product_category_mismatch')

    def _expand_categories(self, ids):
        return set().union(*(self.category_leaves[id] for id in ids))

    def _identity_sort_key(self, index):
        product = self.products[index]
        source = product['source_product_id']
        return source if source is not None else f"backend:{product['id']:020d}"

    def _build_lexical(self):
        if not self.products:
            self.field_terms = []
            self.postings = {}
            return
        analyzed = batch_tokens(text for p in self.products for text in self._fields(p))
        fields = [[Counter(terms) for terms in analyzed[i:i+5]] for i in range(0, len(analyzed), 5)]
        self.field_terms = [tuple(frozenset(field) for field in product_fields) for product_fields in fields]
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
            raise InvalidSearchFilter("UNKNOWN_CATEGORY", "카테고리를 확인해 주세요: " + ', '.join(map(str, sorted(unknown))))
        unknown_brands = {normalize(b) for b in f.brands + f.exclude_brands} - self.brand_keys
        if unknown_brands:
            raise InvalidSearchFilter("UNKNOWN_BRAND", "브랜드 목록에서 선택해 주세요: " + ', '.join(sorted(unknown_brands)))

    def _eligible(self, request):
        f = request.filters
        mask = np.ones(len(self.products), dtype=bool)
        if f.min_price is not None: mask &= self.prices >= f.min_price
        if f.max_price is not None: mask &= self.prices <= f.max_price
        if f.category_ids: mask &= np.isin(self.categories, list(self._expand_categories(f.category_ids)))
        if f.exclude_category_ids: mask &= ~np.isin(self.categories, list(self._expand_categories(f.exclude_category_ids)))
        if f.brands: mask &= np.isin(self.brands, [normalize(b) for b in f.brands])
        if f.exclude_brands: mask &= ~np.isin(self.brands, [normalize(b) for b in f.exclude_brands])
        if f.product_types: mask &= np.isin(self.types, f.product_types)
        if f.availability == 'available_or_unknown':
            mask &= np.isin(self.availability, ['available','unknown'])
        elif f.availability != 'any':
            mask &= self.availability == f.availability
        for id in f.exclude_product_ids:
            if id in self.by_id: mask[self.by_id[id]] = False
        return mask

    def _top_candidates(self, scores, mask, limit=100):
        eligible = np.flatnonzero(mask)
        if eligible.size > limit:
            # Keep all ties at the boundary; slicing an arbitrary argpartition
            # result could silently change which equal-score products qualify.
            values = scores[eligible]
            boundary = np.partition(values, values.size-limit)[values.size-limit]
            eligible = eligible[values >= boundary]
        order = np.lexsort((self.identity_ranks[eligible], -scores[eligible]))
        return eligible[order[:limit]].tolist()

    def _rank(self, request, mask, vector):
        started = time.perf_counter()
        lexical_scores = np.zeros(len(self.products), dtype=np.float32)
        dense_scores = np.zeros(len(self.products), dtype=np.float32)
        query = normalize(request.query)
        query_terms = Counter(tokens(query)) if query else Counter()
        preferred = self._expand_categories(request.preferences.preferred_category_ids)
        downrank = self._expand_categories(request.preferences.downrank_category_ids)
        lexical, dense, fused = [], [], {}
        if query:
            if request.mode != 'dense':
                for token, count in query_terms.items():
                    posting = self.postings.get(token)
                    if posting is not None:
                        idx, weights = posting
                        lexical_scores[idx] += weights * min(count, 2)
                lexical = self._top_candidates(lexical_scores, mask & (lexical_scores > 0))
            if vector is not None:
                dense_scores = self.vectors @ vector
                dense = self._top_candidates(dense_scores, mask)
            for ranking in [lexical, dense]:
                for rank, i in enumerate(ranking, 1):
                    fused[i] = fused.get(i, 0.) + 1 / (30 + rank)
            for i in fused:
                category = self.products[i]['category_id']
                if category in preferred: fused[i] *= 1.10
                if category in downrank: fused[i] *= .75
            order = sorted(fused, key=lambda i: (-fused[i], -float(lexical_scores[i]), self._identity_sort_key(i)))
        else:
            order = [i for i in self.browse_order if mask[i]]
            # Explicit preferences apply to browsing too, with stable order as the tie breaker.
            order.sort(key=lambda i: (self.products[i]['category_id'] in downrank,
                                     -(self.products[i]['category_id'] in preferred)))
        lex_ranks = {i:r for r,i in enumerate(lexical,1)}
        den_ranks = {i:r for r,i in enumerate(dense,1)}
        fields_labels = ['상품명','브랜드','분류·종류','설명·속성','태그']
        qt = set(query_terms)
        hits = []
        for rank, i in enumerate(order[request.offset:request.offset+request.limit], request.offset+1):
            assert mask[i], "hard_filter_violation"
            p = self.products[i]
            summary = self._summary(p)
            summary.update({'rank':rank,'score':round(fused.get(i,0.),7),
                'lexicalScore':round(float(lexical_scores[i]),4),'denseScore':round(float(dense_scores[i]),5) if vector is not None else None,
                'lexicalRank':lex_ranks.get(i),'denseRank':den_ranks.get(i),
                'matchedFields':[label for label,terms in zip(fields_labels,self.field_terms[i]) if qt.intersection(terms)]})
            hits.append(summary)
        return {'hits':hits, 'eligibleCount':int(mask.sum()), 'candidateCount':len(order),
                'hasMore':request.offset+len(hits)<len(order),
                'nextOffset':request.offset+len(hits) if request.offset+len(hits)<len(order) else None,
                'status':'OK' if order else 'NO_MATCH',
                'retrievalMs':round((time.perf_counter()-started)*1000,2)}

    def _summary(self, p):
        return {'productId':p['id'],'sourceProductId':p['source_product_id'],'name':p['name'],'brand':p['brand'],
                'categoryId':p['category_id'],'sourceCategoryId':p['source_category_id'],'parentCategoryId':p['parent_category_id'],
                'category':p['category'],'categoryGroup':p['category_group'],
                'price':p['price'],'description':p['description'][:300], 'productType':p['product_type'],
                'availability':p['availability'],'image':p['image'],'imageLarge':p['image_large'],'imageFallback':p['image_fallback']}

    def _check_snapshot(self, snapshot):
        if snapshot is not None and snapshot != self.snapshot_id:
            raise SnapshotMismatch("카탈로그 버전이 변경되었습니다. 검색을 다시 실행해 주세요.")

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
        request = ProductRequest(ids=ids, snapshot_id=snapshot)
        ids = request.ids
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
        categories=[{'id':c['category_id'],'name':c['category'],'group':c['category_group'],
                     'parentId':c['parent_id'],'sourceCategoryId':c['source_category_id'],'count':counts[c['category_id']]}
                    for c in self.catalog['taxonomy']['categories'] if c['parent_id'] is not None]
        groups=[{'id':c['category_id'],'name':c['category'],'sourceCategoryId':c['source_category_id'],
                 'count':sum(counts[id] for id in self.category_leaves[c['category_id']])}
                for c in self.catalog['taxonomy']['categories'] if c['parent_id'] is None]
        brands=Counter(p['brand'] for p in self.products)
        return {'snapshotId':self.snapshot_id,'algorithm':ALGORITHM,'productCount':len(self.products),
            'lexicalTokenizer':dict(TOKENIZER_INFO),
            'categories':categories,'groups':groups,'brands':[{'name':b,'count':n} for b,n in sorted(brands.items())],
            'productTypes':[{'id':t,'count':n} for t,n in Counter(self.types.tolist()).items()],
            'model':self.manifest['model'],'dimensions':768,'maxTokens':self.manifest['max_tokens'],
            'availabilityNote':self.catalog['availability_note'],'source':self.catalog['source'],
            'exportGeneratedAt':self.catalog.get('export_generated_at')}

    def close(self):
        self.pool.shutdown(wait=True)
