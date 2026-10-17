"""Manually fetch, validate, embed and atomically publish a Backend export.

One writer on one host. This command does not install a periodic scheduler.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from search.export_catalog import project, encode
from search.encoder_contract import encoder_contract, initial_manifest
from search.identity import load_id_mapping
from search.service import SearchService
from search.snapshots import active_data_dir, sync_lock, write_bytes, publish



def encode_documents(root, texts, directory):
    if not texts: return np.empty((0,768),dtype=np.float32), []
    input_path, output_path = directory/'embedding-input.json', directory/'embedding-output.f32'
    input_path.write_bytes(encode(texts))
    try:
        subprocess.run(['node',str(root/'embedding/documents.mjs'),str(input_path),str(output_path)],
            cwd=root/'embedding', check=True, timeout=3600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vectors = np.frombuffer(output_path.read_bytes(),dtype=np.float32).reshape(len(texts),768).copy()
        counts = json.loads(Path(str(output_path)+'.json').read_text())
        return vectors, counts
    finally:
        for path in [input_path,output_path,Path(str(output_path)+'.json')]: path.unlink(missing_ok=True)


def sync(root, fetch, *, embed=encode_documents, fingerprint=encoder_contract, apply=False, initial_only=False):
    root = Path(root)
    data = root/'data'
    with sync_lock(data):
        old_dir = active_data_dir(data)
        existing = any((old_dir/name).exists() for name in ('catalog.json','manifest.json','vectors.f32')) or (data/'CURRENT').exists()
        if existing:
            if initial_only:
                raise ValueError('Refusing to replace an existing catalog with sample data')
            # A partial/corrupt installation is an error, never an empty catalog.
            old_raw = (old_dir/'catalog.json').read_bytes()
            old_catalog = json.loads(old_raw)
            old_manifest = json.loads((old_dir/'manifest.json').read_text())
            old_vectors_raw = (old_dir/'vectors.f32').read_bytes()
            if hashlib.sha256(old_raw).hexdigest()!=old_manifest['catalog_sha256'] or hashlib.sha256(old_vectors_raw).hexdigest()!=old_manifest['vectors_sha256']:
                raise ValueError('Active catalog/vector hash mismatch')
            if [p['id'] for p in old_catalog['products']]!=old_manifest['product_ids']:
                raise ValueError('Active vector order mismatch')
        else:
            old_catalog = {'products': [], 'taxonomy': {'categories': []}}
            old_manifest = initial_manifest(root)
            old_vectors_raw = b''
        product_map = {p['source_product_id']:p['id'] for p in old_catalog['products'] if p.get('source_product_id')}
        category_map = {c['source_category_id']:c['category_id'] for c in old_catalog.get('taxonomy',{}).get('categories',[]) if c.get('source_category_id')}
        if (data/'product-id-map.jsonl').exists():
            product_map,_ = load_id_mapping(data/'product-id-map.jsonl')
        if (data/'category-id-map.jsonl').exists():
            category_map,_ = load_id_mapping(data/'category-id-map.jsonl','category')
        # Fetch only after the writer lock has been acquired.
        export, categories = fetch()
        catalog = project(export,categories,old_catalog,product_map,category_map)
        signature = fingerprint(root,old_manifest)
        reuse_ok = old_manifest.get('encoder_fingerprint')==signature and old_catalog.get('document_contract')=='backend-export/1'
        old_indices = {p['id']:i for i,p in enumerate(old_catalog['products'])}
        changed, reused = [], []
        for i,p in enumerate(catalog['products']):
            old_index = old_indices.get(p['id'])
            previous = old_catalog['products'][old_index] if old_index is not None else None
            if reuse_ok and previous and previous['document']==p['document']:
                reused.append((i,old_index))
            else:
                changed.append(i)
        result = {'products':len(catalog['products']), 'removed':len(set(old_indices)-{p['id'] for p in catalog['products']}),
            'embedDocuments':len(changed), 'reuseVectors':len(reused), 'generatedAt':catalog['export_generated_at'], 'applied':apply}
        if not apply: return result
        generations = data/'generations'
        generations.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.staging-',dir=generations))
        try:
            vectors = np.empty((len(catalog['products']),768),dtype=np.float32)
            counts = [0]*len(vectors)
            old_vectors = np.frombuffer(old_vectors_raw,dtype=np.float32).reshape(len(old_indices),768)
            for new,old in reused:
                vectors[new]=old_vectors[old]
                counts[new]=old_manifest['token_counts'][old]
            new_vectors,new_counts = embed(root,[catalog['products'][i]['document'] for i in changed],temporary)
            if np.shape(new_vectors)!=(len(changed),768) or len(new_counts)!=len(changed):
                raise ValueError('Embedding count or dimensions mismatch')
            for i,v,count in zip(changed,new_vectors,new_counts):
                if type(count) is not int or count<1: raise ValueError('Invalid token count')
                vectors[i],counts[i]=v,count
            if not np.isfinite(vectors).all() or np.any(np.linalg.norm(vectors,axis=1)==0):
                raise ValueError('Invalid vectors')
            raw,vector_bytes = encode(catalog),vectors.tobytes()
            manifest = dict(old_manifest)
            manifest.update(catalog_sha256=hashlib.sha256(raw).hexdigest(),vectors_sha256=hashlib.sha256(vector_bytes).hexdigest(),
                product_ids=[p['id'] for p in catalog['products']], source_product_ids=[p['source_product_id'] for p in catalog['products']],
                token_counts=counts,truncated_documents=sum(c>1024 for c in counts),encoder_fingerprint=signature)
            for name,content in [('catalog.json',raw),('vectors.f32',vector_bytes),('manifest.json',encode(manifest))]:
                write_bytes(temporary/name,content)
            # Construct the complete lexical+dense index before publishing anything.
            candidate=SearchService(temporary)
            published_snapshot = candidate.snapshot_id
            candidate.close()
            name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:12]
            result.update(snapshotId=published_snapshot,generation=name)
            write_bytes(temporary/'sync-result.json',encode(result))
            temporary.rename(generations/name)
            publish(data,name)
            return result
        finally:
            if temporary.exists(): shutil.rmtree(temporary)


def fetcher(export_url, categories_url, token):
    def fetch():
        headers = {'Accept':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        with httpx.Client(timeout=httpx.Timeout(60,connect=5),trust_env=False,follow_redirects=False) as client:
            responses=[]
            for url in [export_url,categories_url]:
                response=client.get(url,headers=headers)
                # Avoid emitting URLs/headers from HTTP exceptions, which may hold credentials.
                if response.status_code!=200: raise RuntimeError(f'Backend returned HTTP {response.status_code}')
                responses.append(response.json())
            return responses
    return fetch


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--export-url')
    source.add_argument('--export-file',type=Path,help='Saved export response or synthetic example')
    parser.add_argument('--categories-url',help='Backend category hierarchy API, same trusted server')
    parser.add_argument('--categories-file',type=Path)
    parser.add_argument('--token-env',default='BACKEND_SERVICE_TOKEN',help='Environment variable name; never pass the token itself')
    parser.add_argument('--apply',action='store_true',help='Default validates and reports expected changes without embedding or publishing')
    parser.add_argument('--initial-only',action='store_true',help='Refuse to replace an existing catalog')
    args=parser.parse_args()
    if args.export_url:
        if not args.categories_url or args.categories_file:
            parser.error('--export-url requires --categories-url and no --categories-file')
        token=os.getenv(args.token_env)
        if not token:
            parser.error('Set the service token environment variable for Backend requests')
        fetch=fetcher(args.export_url,args.categories_url,token)
    else:
        if not args.categories_file or args.categories_url:
            parser.error('--export-file requires --categories-file and no --categories-url')
        def fetch():
            return json.loads(args.export_file.read_text()),json.loads(args.categories_file.read_text())
    try:
        result=sync(args.root,fetch,apply=args.apply,initial_only=args.initial_only)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:
        # Validation errors can contain input data; retain only the error type in CLI output.
        print(f'Catalog sync failed ({type(exc).__name__}); active catalog retained unless publication already completed.',file=sys.stderr)
        sys.exit(1)
