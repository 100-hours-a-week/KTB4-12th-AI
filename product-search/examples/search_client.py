"""Run the HTTP integration sequence against a locally running QA search service."""
import argparse
import json
import httpx

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:4325')
    parser.add_argument('--query', default='텀블러')
    parser.add_argument('--max-price', type=int, default=50000)
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        # 1. 서버 준비 상태와 활성 스냅샷을 확인한다.
        response = client.get('/healthz')
        response.raise_for_status()
        health = response.json()
        # 2. Backend 숫자 ID 대신 현 카탈로그의 source ID가 반환된다.
        response = client.post('/api/search', json={
            'query': args.query, 'filters': {'maxPrice':args.max_price},
            'mode':'hybrid', 'limit':5,
        })
        response.raise_for_status()
        search = response.json()
        # 3. 같은 스냅샷의 원문/속성을 조회한다. ids=[]는 HTTP에서 허용하지 않는다.
        source_ids = [p['id'] for p in search['hits']]
        details = {'products':[], 'missingIds':[]}
        if source_ids:
            response = client.post('/api/products', json={
                'ids':source_ids, 'snapshotId':search['snapshotId'],
            })
            response.raise_for_status()
            details = response.json()
        print(json.dumps({
            'ready':health['status'], 'snapshotId':search['snapshotId'],
            'searchId':search['searchId'], 'status':search['status'],
            'hits':len(search['hits']), 'details':len(details['products']),
            'missingIds':details['missingIds'],
            'identifiers':[{'sourceProductId':p['id'], 'backendProductId':p['backendProductId'],
                            'availability':p['availability']} for p in search['hits']],
        },ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
