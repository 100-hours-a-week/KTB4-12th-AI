"""Import explicitly supplied local catalog/assets into the isolated AI DB.

No network, image download, paid calls or source changes. Missing media remains
NULL and is counted; source URLs are provenance, never hotlink display paths.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'product-search'))
from catalog_db import encode, projection, materialize, schema_version
from search.encoder_contract import encoder_contract


def import_local(url, runtime, data, package, audit, media):
    runtime, data, package, media = map(Path, [runtime,data,package,media])
    source_raw=(data/'catalog.json').read_bytes(); source=json.loads(source_raw)
    old=json.loads((data/'manifest.json').read_text()); vectors=(data/'vectors.f32').read_bytes()
    if hashlib.sha256(source_raw).hexdigest()!=old['catalog_sha256'] or hashlib.sha256(vectors).hexdigest()!=old['vectors_sha256']:
        raise ValueError('Local source catalog/vector hashes differ')
    products=source['products']; packaged={p['sourceProductId']:p for p in [json.loads(line) for line in (package/'data/products.jsonl').read_text().splitlines() if line.strip()]}
    if [p['id'] for p in products]!=old['product_ids'] or {p['source_product_id'] for p in products}!=set(packaged):
        raise ValueError('Product/source/vector row mapping differs')
    audit=json.loads(Path(audit).read_text())
    required_images=[image for row in audit['products'] for image in row['images'] if image['kind'] in {'representative','additional'}]
    missing_image_urls={image['sourceUrl'] for image in required_images if not image.get('existingOriginal')}
    if audit['summary']['decodeOrHashFailures'] or audit['summary']['missingFiles']:
        raise ValueError('Existing media is not verified')
    media.mkdir(parents=True,exist_ok=True)
    path_map={}
    for item in audit['files']:
        path=Path(item['path'])
        name=path.name if path.suffix.lower() in {'.avif','.webp'} else 'original-'+item['sha256']+path.suffix.lower()
        target=media/name
        if not target.exists():
            os.link(path,target)  # Same local disk, no full copy or source mutation.
        if target.stat().st_size!=item['bytes']:
            raise ValueError('Media hardlink differs')
        path_map[str(path)]='/media/'+name
    version_id=uuid.uuid4(); package_id='local-import-'+version_id.hex
    whitespace_normalized=0
    with psycopg.connect(url,row_factory=dict_row) as db:
        if db.execute('SELECT current_user AS name').fetchone()['name']!='fixture_owner' or not db.execute('SELECT run_identity FROM ai_catalog.integration_marker WHERE id').fetchone():
            raise ValueError('Real catalog import requires the isolated integration database')
        modern = schema_version(db)
        category_ids = {c['source_category_id']:c['category_id'] for c in source['taxonomy']['categories']}
        for c in source['taxonomy']['categories']:
            parent=next((x['source_category_id'] for x in source['taxonomy']['categories'] if x['category_id']==c['parent_id']),None)
            if modern:
                db.execute('''INSERT INTO ai_catalog.categories(id,parent_id,name,level,product_count,taxonomy_version)
                    VALUES(%s,%s,%s,%s,0,%s)''',(c['category_id'],category_ids[parent] if parent else None,c['category'],2 if parent else 1,package_id))
                db.execute('INSERT INTO ai_catalog.integration_category_sources(category_id,source_category_id) VALUES(%s,%s)',(c['category_id'],c['source_category_id']))
            else:
                db.execute('''INSERT INTO ai_catalog.categories(source_category_id,parent_source_category_id,name,level,product_count,backend_category_id,taxonomy_version)
                    VALUES(%s,%s,%s,%s,0,%s,%s)''', (c['source_category_id'],parent,c['category'],2 if parent else 1,c['category_id'],package_id))
        for p in products:
            original=packaged[p['source_product_id']]
            image=lambda key: '/media/'+Path(p[key]).name if p.get(key) else None
            if any(p[k]!=original[v] for k,v in [('name','productName'),('brand','brandName'),('price','unitPrice'),('description','description'),('product_type','productType')]):
                raise ValueError('Source package essential fields differ from normalized catalog')
            if p['attributes']!=original['attributes']:
                if p['attributes'].keys()!=original['attributes'].keys() or any(not isinstance(v,str) or not isinstance(original['attributes'][k],str) or ' '.join(v.split())!=' '.join(original['attributes'][k].split()) for k,v in p['attributes'].items()):
                    raise ValueError('Source package attribute facts differ')
                whitespace_normalized+=1
            product_sql = '''INSERT INTO ai_catalog.products(source_product_id,backend_product_id,name,brand,source_category_id,product_kind,product_type,description,attributes,unit_price,list_price,currency,stock_quantity,availability,source_provider,source_product_url,source_image_url,image_asset_id,package_id,display_image_path,display_image_large_path,display_image_fallback_path,search_document,normalized_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'''
            if modern:
                product_sql=product_sql.replace('backend_product_id','id').replace('source_category_id','category_id')
            db.execute(product_sql,
                (p['source_product_id'],p['id'],p['name'],p['brand'],category_ids[p['source_category_id']] if modern else p['source_category_id'],p['kind'],p['product_type'],p['description'],Jsonb(original['attributes']),p['price'],original['listPrice'],original['currency'],original['stockQuantity'],p['availability'],original['source']['provider'],p['product_url'],original['source']['imageUrl'],p.get('image_key'),package_id,image('image'),image('image_large'),image('image_fallback'),p['document'],p['normalized_name']))
        for row in audit['products']:
            with db.cursor() as cursor:
                cursor.executemany('INSERT INTO ai_catalog.product_media(source_product_id,kind,ordinal,source_url,media_path) VALUES(%s,%s,%s,%s,%s)',
                    [(row['sourceProductId'],i['kind'],i['order'],i['sourceUrl'],path_map.get(i['existingOriginal']['path']) if i.get('existingOriginal') else None) for i in row['images'] if i['kind']!='detail'])
        db.execute('INSERT INTO ai_catalog.catalog_versions(id,package_id,taxonomy_version,product_count,category_count,source_sha256) VALUES(%s,%s,%s,%s,%s,%s)',
            (version_id,package_id,package_id,len(products),len(source['taxonomy']['categories']),Jsonb({'catalog':old['catalog_sha256'],'idMapping':source.get('id_mapping_provenance')})))
        version=db.execute('SELECT * FROM ai_catalog.catalog_versions WHERE id=%s',(version_id,)).fetchone()
        catalog=projection(db,version)
        for actual,expected in zip(catalog['products'],products):
            if any(actual[k]!=expected[k] for k in ['id','source_product_id','name','brand','price','description','document','normalized_name','category_id','kind','product_type','availability']) or actual['attributes']!=packaged[expected['source_product_id']]['attributes']:
                raise ValueError('Canonical projection differs from source product')
        manifest=dict(old);manifest.update(ai_catalog_version=str(version_id),catalog_sha256=hashlib.sha256(encode(catalog)).hexdigest(),source_product_ids=[p['source_product_id'] for p in products])
        fingerprint=encoder_contract(runtime,manifest)
        if old.get('encoder_fingerprint') and old['encoder_fingerprint']!=fingerprint:
            raise ValueError('Provided encoder fingerprint differs; rebuild vectors explicitly')
        manifest['encoder_fingerprint']=fingerprint
        db.execute('INSERT INTO ai_catalog.search_generations(catalog_version,manifest,vectors) VALUES(%s,%s,%s)',(version_id,Jsonb(manifest),vectors))
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=false WHERE is_active')
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=true WHERE id=%s',(version_id,))
    binding=materialize(url,runtime)
    return {'binding':binding,'products':len(products),'essentialFieldMismatches':0,'sourceIdsMatched':True,'vectorsReusedAndHashVerified':True,'attributeWhitespaceNormalizedProducts':whitespace_normalized,
        'legacyManifestHasEncoderFingerprint':bool(old.get('encoder_fingerprint')),'vectorInputDocumentsUnchanged':True,'mediaFilesLinked':len(list(media.iterdir())),
        'mediaComplete':not missing_image_urls,'missingImageUrls':len(missing_image_urls),'requiredImageRoles':['representative','additional'],
        'missingUrlCountIsContentUniqueFileCount':False,'idMappingProvenance':source.get('id_mapping_provenance')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ['runtime','data','package','audit','media']:p.add_argument('--'+key,type=Path,required=True)
    args=p.parse_args()
    print(json.dumps(import_local(os.environ['CATALOG_OWNER_DATABASE_URL'],args.runtime,args.data,args.package,args.audit,args.media)))
